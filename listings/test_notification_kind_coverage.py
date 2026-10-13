# -*- coding: utf-8 -*-
"""通知类型、偏好开关与用户手上的那一栏——版图第一次被运营读到。

这一轮量的不是解析函数，是三件更根本的事。

第一件是**三个数不相等**。``KIND_CHOICES`` 有 24 个值、``NotificationPreference`` 上有
26 个布尔开关、``NotificationPreferenceForm`` 里摆出来 27 个字段。三个表各自自洽，所以差额
一直被「看起来像」吃掉了。测试必须钉住这条边界：版图从三个来源**现读**，不硬编码名单——
硬编码的名单会和代码走散，而那正是本模块要报的问题。

第二件是**一个正在被发送、却没有名字的类型**。``mutual_aid_feedback`` 在 ``views.py`` 的
两处被真正发送，偏好表上有它的开关，而 ``KIND_CHOICES`` 里没有它。后果不止是看板上显示
英文：``notification_list`` 的类型筛选器拿 ``KIND_CHOICES`` 当白名单，所以**这一类通知，
用户在自己的通知中心里筛不出来**。测试要确认它落在「没有名字」那一行，且标签回退成英文
键名——这正是 ``kind_labels.get(kind, kind)`` 的 fallback 第一次被触发。

第三件是**一个用户关不掉的开关**。``meeting_incident`` 模型上有、默认开着、真的在发，而
表单的 27 个字段里没有它。模板是 ``{% for field in form.visible_fields %}``，表单里没有的
字段页面上就没有那一行。测试要确认它落在「用户关不掉」那一行，且它**有**偏好开关——少了
这一列，「用户关不掉」和「连开关都没有」会被读成同一件事。

本模块只报告：不加迁移、不改 ``KIND_CHOICES``、不改 ``NotificationPreference``、不改表单
字段列表。那两个窟窿的修法各是一行代码，但那是修复，不是度量。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Notification, NotificationPreference
from .notification_kind_coverage import (
    MIN_BUCKET_SAMPLE,
    _form_switch_fields,
    _preference_switch_fields,
    build_notification_kind_coverage,
)


def local_noon():
    return timezone.localtime().replace(hour=12, minute=0, second=0, microsecond=0)


def make_user(username):
    return User.objects.create_user(
        username=username, password='safe-password-123',
    )


def make_notification(recipient, **kwargs):
    """建一条通知，再把自动时间列写回夹具指定的值。

    ``created_at`` 是 ``auto_now_add``，创建时传值会被忽略，所以建完之后再用 update 写回。
    否则所有通知都落在「现在」，统计窗口和阅读情况全都读不出来。
    """
    notification = Notification.objects.create(
        recipient=recipient,
        kind=kwargs.pop('kind', 'message_received'),
        title=kwargs.pop('title', '一条通知'),
        message=kwargs.pop('message', '内容'),
    )
    fields = {}
    for name in ('created_at', 'read_at'):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    for name in ('is_read',):
        if name in kwargs:
            fields[name] = kwargs.pop(name)
    if fields:
        Notification.objects.filter(pk=notification.pk).update(**fields)
        notification.refresh_from_db()
    return notification


class KindCoverageSetupTests(TestCase):
    """夹具、空态和统计窗口。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('kind-coverage-user')

    def test_an_empty_period_reports_no_data_rather_than_zero(self):
        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['current']['total_sent'], 0)
        self.assertIsNone(report['current']['total_sent'] or None)
        self.assertIn('还没有通知数据', report['summary'])

    def test_an_unknown_period_falls_back_to_thirty_days(self):
        report = build_notification_kind_coverage(days=999, now=self.now)
        self.assertEqual(report['period_days'], 30)

    def test_a_notification_created_before_the_window_is_excluded(self):
        make_notification(self.user, created_at=self.now - timedelta(days=45))

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertFalse(report['has_data'])
        self.assertEqual(report['current']['total_sent'], 0)


class LandscapeTests(TestCase):
    """版图：三个来源现读，不硬编码。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('kind-landscape-user')

    def test_the_landscape_covers_the_union_of_choices_and_switches(self):
        """行取自 ``KIND_CHOICES`` 与偏好开关的并集，不是只报其中一边。

        只报一边就等于假设另一边是齐的，而本模块存在的理由正是这个假设不成立。
        """
        from .models import Notification

        report = build_notification_kind_coverage(days=30, now=self.now)
        kinds = {row['kind'] for row in report['landscape']}
        switches = set(_preference_switch_fields())
        choices = {value for value, _label in Notification.KIND_CHOICES}

        self.assertEqual(kinds, choices | switches)
        # mutual_aid_feedback 在开关里、不在 KIND_CHOICES 里，必须作为一行出现。
        self.assertIn('mutual_aid_feedback', kinds)
        self.assertIn('meeting_incident', kinds)

    def test_the_landscape_reads_three_sources_from_code_not_a_hardcoded_list(self):
        """三个来源全部现读：模型、``KIND_CHOICES``、表单。

        硬编码的名单会和代码走散。测试直接比对面两份现读的集合，确认版图跟得上代码。
        """
        from .models import Notification

        report = build_notification_kind_coverage(days=30, now=self.now)
        for row in report['landscape']:
            self.assertEqual(
                row['has_choice_label'],
                row['kind'] in dict(Notification.KIND_CHOICES),
            )
            self.assertEqual(
                row['has_preference_switch'],
                row['kind'] in set(_preference_switch_fields()),
            )
            self.assertEqual(
                row['switch_in_form'],
                row['kind'] in _form_switch_fields(),
            )


class UnnamedKindTests(TestCase):
    """没有名字的类型：``mutual_aid_feedback``。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('kind-unnamed-user')

    def test_mutual_aid_feedback_is_sent_but_has_no_choice_label(self):
        """一个正在被发送、正在被接收、正在被关掉的类型，在用户界面上没有名字。"""
        make_notification(
            self.user, kind='mutual_aid_feedback',
            created_at=self.now - timedelta(days=2), read_at=self.now - timedelta(days=1),
            is_read=True,
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        unnamed = {row['kind']: row for row in report['unnamed_rows']}
        self.assertIn('mutual_aid_feedback', unnamed)

        row = unnamed['mutual_aid_feedback']
        self.assertEqual(row['label'], 'mutual_aid_feedback')
        self.assertEqual(row['sent_count'], 1)
        self.assertEqual(row['read_count'], 1)
        self.assertTrue(row['sent_in_period'])

    def test_the_unnamed_kind_falls_back_to_the_raw_key_as_its_label(self):
        """``kind_labels.get(kind, kind)`` 的 fallback 第一次被触发。

        这正是 ``notification_response`` 和 ``notification_snooze_return`` 里那句从未被
        走到过的分支。看板上它会显示成英文键名，用户在自己的通知中心里也筛不出它。
        """
        from .models import Notification

        make_notification(self.user, kind='mutual_aid_feedback',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        row = next(r for r in report['unnamed_rows'] if r['kind'] == 'mutual_aid_feedback')

        self.assertNotIn('mutual_aid_feedback', dict(Notification.KIND_CHOICES))
        self.assertEqual(row['label'], 'mutual_aid_feedback')

    def test_a_type_with_a_label_never_lands_in_the_unnamed_rows(self):
        make_notification(self.user, kind='order_created',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertEqual(
            [row['kind'] for row in report['unnamed_rows']
             if row['kind'] == 'order_created'],
            [],
        )


class UnreachableSwitchTests(TestCase):
    """用户关不掉的开关：``meeting_incident``。"""

    def setUp(self):
        self.now = local_noon()
        self.user = make_user('kind-unreachable-user')

    def test_meeting_incident_has_a_switch_that_is_not_in_the_form(self):
        """用户收到了，但偏好页面上没有一个开关能关掉它。

        关键是它**有**偏好开关——少了这一列，「用户关不掉」和「连开关都没有」会被读成
        同一件事，而这两件事的修法完全不同。
        """
        make_notification(
            self.user, kind='meeting_incident',
            created_at=self.now - timedelta(days=2),
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        unreachable = {row['kind']: row for row in report['unreachable_rows']}
        self.assertIn('meeting_incident', unreachable)

        row = unreachable['meeting_incident']
        self.assertEqual(row['sent_count'], 1)
        self.assertTrue(row['sent_in_period'])

        landscape = {r['kind']: r for r in report['landscape']}
        incident = landscape['meeting_incident']
        self.assertTrue(incident['has_preference_switch'])
        self.assertTrue(incident['has_choice_label'])
        self.assertFalse(incident['switch_in_form'])

    def test_every_other_sent_kind_is_reachable_from_the_form(self):
        """其余类型的开关都在用户手上，唯独交付预约异常是 Silent 的例外。"""
        make_notification(self.user, kind='order_created',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertNotIn(
            'order_created', {row['kind'] for row in report['unreachable_rows']},
        )


class PreferenceStateTests(TestCase):
    """偏好的实际状态：只统计发过的类型，不按用户报。"""

    def setUp(self):
        self.now = local_noon()

    def test_only_kinds_that_were_actually_sent_appear(self):
        """一个从没被发送过的类型，它的开关开着或关着都不产生任何后果。"""
        user = make_user('kind-pref-only-sent')
        make_notification(user, kind='order_created',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        kinds = {row['kind'] for row in report['preference_rows']}
        self.assertEqual(kinds, {'order_created'})

    def test_a_switched_off_kind_is_counted_against_its_recipients(self):
        """关掉了的类型按接收人数报，不按用户排名、不报任何人的用户名。"""
        user = make_user('kind-pref-off')
        make_notification(user, kind='order_created',
                          created_at=self.now - timedelta(days=1))
        preference, _ = NotificationPreference.objects.get_or_create(user=user)
        preference.order_created = False
        preference.save(update_fields=['order_created'])

        report = build_notification_kind_coverage(days=30, now=self.now)
        row = next(r for r in report['preference_rows'] if r['kind'] == 'order_created')
        self.assertEqual(row['recipient_count'], 1)
        self.assertEqual(row['switched_off'], 1)
        self.assertEqual(row['switched_off_rate'], 100.0)

    def test_a_user_without_a_preference_row_counts_as_switched_on(self):
        """没有偏好行的用户走默认值（全开）。

        ``create_notification`` 第一件事就是 ``get_or_create``，所以「没有行」等价于
        「没碰过，全开」。不建偏好行就统计，确认默认值不会被读成关闭。
        """
        user = make_user('kind-pref-default')
        make_notification(user, kind='order_created',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        row = next(r for r in report['preference_rows'] if r['kind'] == 'order_created')
        self.assertEqual(row['switched_off'], 0)
        self.assertEqual(row['switched_off_rate'], 0.0)

    def test_the_unnamed_kind_also_gets_a_preference_row(self):
        """没有中文名的类型照样有开关、照样被关——它只是没有名字，不是没有行为。"""
        user = make_user('kind-pref-unnamed')
        make_notification(user, kind='mutual_aid_feedback',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        kinds = {row['kind'] for row in report['preference_rows']}
        self.assertIn('mutual_aid_feedback', kinds)


class ReadCountTests(TestCase):
    """已读要求 ``read_at`` 有值。"""

    def setUp(self):
        self.now = local_noon()

    def test_a_read_notification_without_a_timestamp_is_not_counted_as_read(self):
        """``is_read=True`` 而 ``read_at IS NULL`` 的是阅读时间上线之前标记的老数据。

        延后通知回归模块已经如实报出过这一档；本模块不把它算进已读，也不并进未读，
        如实漏过去——并进任何一档都是编一个库里没有的事实。
        """
        user = make_user('kind-read-no-stamp')
        make_notification(
            user, kind='order_created', created_at=self.now - timedelta(days=2),
            is_read=True,
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        row = next(r for r in report['preference_rows'] if r['kind'] == 'order_created')
        self.assertEqual(row['read_count'], 0)

    def test_a_read_notification_with_a_timestamp_is_counted(self):
        user = make_user('kind-read-with-stamp')
        make_notification(
            user, kind='order_created', created_at=self.now - timedelta(days=2),
            read_at=self.now - timedelta(days=1), is_read=True,
        )

        report = build_notification_kind_coverage(days=30, now=self.now)
        row = next(r for r in report['preference_rows'] if r['kind'] == 'order_created')
        self.assertEqual(row['read_count'], 1)


class SummaryTests(TestCase):
    """一段人话，说明版图此刻齐不齐。"""

    def setUp(self):
        self.now = local_noon()

    def test_an_empty_period_says_the_landscape_needs_notifications_first(self):
        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertIn('还没有通知数据', report['summary'])

    def test_a_period_with_both_gaps_names_both_of_them(self):
        """两个窟窿都要被点到名，一个都不漏。"""
        user = make_user('kind-summary-both')
        make_notification(user, kind='mutual_aid_feedback',
                          created_at=self.now - timedelta(days=1))
        make_notification(user, kind='meeting_incident',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertIn('没有中文名', report['summary'])
        self.assertIn('不在偏好页面上', report['summary'])

    def test_the_two_gaps_are_code_facts_not_period_artifacts(self):
        """两个窟窿是代码事实，跟这个周期发了什么、发了多少都无关。

        一开始这里写的是「一个两种窟窿都没有的周期，summary 应该说三种表是对齐的」。
        那个预期不成立：只要 ``mutual_aid_feedback`` 不在 ``KIND_CHOICES`` 里、
        ``meeting_incident`` 不在表单里，两个 gap 就恒在——这正是本模块要报的事，
        换任何一个周期、发任何一种通知都绕不过去。所以改成钉住这条边界：
        干净的周期照样报两个 gap，且报的是类型名，不是条数。
        """
        user = make_user('kind-summary-clean')
        make_notification(user, kind='order_created',
                          created_at=self.now - timedelta(days=1))

        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertNotIn('三种表此刻是对齐的', report['summary'])
        self.assertIn('mutual_aid_feedback', report['summary'])
        self.assertIn('交付预约异常', report['summary'])

    def test_an_empty_period_still_reports_the_gaps_because_they_are_code_facts(self):
        """一条通知都没有的周期，窟窿照样在——它们不是统计量。

        空平台的 summary 走「还没有通知数据」那一支，但两个 gap 的集合不变：
        版图是从代码现读的，不依赖周期里有没有数据。
        """
        report = build_notification_kind_coverage(days=30, now=self.now)
        self.assertIn('还没有通知数据', report['summary'])
        unnamed = {row['kind'] for row in report['unnamed_rows']}
        unreachable = {row['kind'] for row in report['unreachable_rows']}
        self.assertIn('mutual_aid_feedback', unnamed)
        self.assertIn('meeting_incident', unreachable)


class DashboardWiringTests(TestCase):
    """面板与导出：两个窟窿都要出现在运营眼前。"""

    def setUp(self):
        self.now = local_noon()
        self.staff = User.objects.create_superuser(
            username='kind-coverage-staff', email='staff@example.com',
            password='safe-password-123',
        )

    def test_the_dashboard_panel_renders_the_two_gaps(self):
        self.client.force_login(self.staff)
        user = make_user('kind-wiring-user')
        make_notification(user, kind='mutual_aid_feedback',
                          created_at=self.now - timedelta(days=1))
        make_notification(user, kind='meeting_incident',
                          created_at=self.now - timedelta(days=1))

        response = self.client.get('/listings/operations/')
        self.assertEqual(response.status_code, 200)
        self.assertIn('notification_kind_coverage', response.context)
        content = response.content.decode('utf-8')
        self.assertIn('通知类型、偏好开关与用户手上的那一栏', content)
        self.assertIn('mutual_aid_feedback', content)

    def test_the_csv_export_carries_the_same_section(self):
        self.client.force_login(self.staff)
        user = make_user('kind-export-user')
        make_notification(user, kind='mutual_aid_feedback',
                          created_at=self.now - timedelta(days=1))

        response = self.client.get('/listings/operations/export/')
        self.assertEqual(response.status_code, 200)
        content = response.content.decode('utf-8-sig')
        self.assertIn('通知类型、偏好开关与用户手上的那一栏', content)
        self.assertIn('没有名字的类型', content)
