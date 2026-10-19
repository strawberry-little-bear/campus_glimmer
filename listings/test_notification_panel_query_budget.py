# -*- coding: utf-8 -*-
"""三个通知面板的查询预算，和一份逐字段重算的口径 oracle。

`notification_preference_usage`、`notification_preference_bypass_diagnosis`、
`notification_kind_coverage` 原先按「一个偏好字段发一次查询」取数：二十多个开关就是
一百多次往返，而它们报的都是同一批偏好行的不同切面。现在收敛成一次 aggregate。

收敛只改查询的形状，不改任何一个数字。这个模块钉住两件事：

1. **口径不能因为收敛而变**——用一套刻意写得笨拙的逐字段实现当 oracle，逐格比对。
   aggregate 写漏一个条件、把 `exclude` 写成 `filter`，这里先红；
2. **预算不能被拆回去**——把查询数上界写下来。谁觉得「循环里各查一次更清楚」，
   测试先红，然后去改注释里写明的那个有意的例外，而不是把面板拆回 N+1。

运营看板单页原先 690 次查询，这三个面板占 264 次。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.db import connection
from django.db.models import Count, F
from django.db.models.functions import Coalesce
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone

from .models import Category, Item, Notification, NotificationPreference
from .notification_kind_coverage import (
    _period_start, build_notification_kind_coverage,
)
from .notification_preference_bypass_diagnosis import (
    build_notification_preference_bypass_diagnosis,
)
from .notification_preference_usage import (
    _preference_fields, _recently_notified_users_by_kind,
    build_notification_preference_usage,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_category():
    return Category.objects.create(name='查询预算分类')


def make_item(seller, category):
    return Item.objects.create(
        title='查询预算商品', description='通知面板查询预算测试',
        price='10.00', category=category, condition='九成新', seller=seller,
    )


def make_notification(recipient, kind, occurred_at):
    row = Notification.objects.create(
        recipient=recipient, kind=kind, title='t', message='m',
    )
    # created_at 是 auto_now_add，只能回写；last_occurred_at 留空，让 occurred_at
    # 落到 created_at 上，和统一写入路径的形状一致。
    Notification.objects.filter(pk=row.pk).update(created_at=occurred_at)
    return row


def seed_panel_data(now):
    """一批足以让 usage 的四格和 bypass 的「可疑/一致」都非空的数据。

    返回周期的起点，oracle 用它复算窗口。
    """
    category = make_category()
    seller = make_user('budget-seller')
    make_item(seller, category)

    # (用户名后缀, 偏好字段, 是否在窗口内收到过, 偏好行是否写在通知之后)
    # 第三列为 False 表示这个用户根本没有通知行——「没收到过」那一格。
    # 第四列为 True 是「先通知、后关开关」，为 False 是「偏好写得比通知早」。
    plan = (
        ('seen-on', 'order_created', True, False),
        ('seen-off-consistent', 'order_created', True, True),
        ('seen-off-suspicious', 'order_created', True, False),
        ('unseen-on', 'order_created', False, False),
        ('unseen-off', 'order_created', False, False),
        ('seen-on-msg', 'message_received', True, False),
        ('seen-off-suspicious-msg', 'message_received', True, False),
    )
    for suffix, field, in_window, written_after in plan:
        user = make_user('budget-%s' % suffix)
        preference = NotificationPreference.objects.create(user=user, **{field: False})
        if in_window:
            make_notification(user, field, now - timedelta(days=1))
        if not written_after:
            # 把整行偏好的最后写入时间推到通知之前：这一行就是「偏好写入后仍出现」。
            NotificationPreference.objects.filter(pk=preference.pk).update(
                updated_at=now - timedelta(days=2),
            )
    # 一个收到过通知、却从没建过偏好行的用户：四格之外单列的那一格。
    buyer = make_user('budget-no-preference')
    make_notification(buyer, 'order_created', now - timedelta(days=2))
    return now - timedelta(days=30)


class PreferenceUsageOracleTests(TestCase):
    """第一件：usage 的每个数字，和一个字段一次查询的笨实现逐格相同。"""

    def test_four_cells_match_a_naive_per_field_recount(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_preference_usage(days=30, now=now)
        by_field = {row['field']: row for row in result['rows']}
        start = now - timedelta(days=30)

        for field in _preference_fields():
            on = NotificationPreference.objects.filter(**{field: True}).count()
            off = NotificationPreference.objects.filter(**{field: False}).count()
            seen = set(Notification.objects.filter(
                kind=field, created_at__gte=start,
                created_at__lt=now + timedelta(seconds=60),
            ).values_list('recipient_id', flat=True).distinct())
            off_but_seen = NotificationPreference.objects.filter(
                **{field: False, 'user_id__in': seen},
            ).count() if seen else 0
            on_but_unseen = NotificationPreference.objects.filter(
                **{field: True},
            ).exclude(user_id__in=seen).count() if seen else on

            row = by_field[field]
            self.assertEqual(row['on'], on, field)
            self.assertEqual(row['off'], off, field)
            self.assertEqual(row['rows_total'], on + off, field)
            self.assertEqual(row['off_but_seen'], off_but_seen, field)
            self.assertEqual(row['on_but_unseen'], on_but_unseen, field)
            self.assertEqual(row['notified_users'], len(seen), field)

    def test_quiet_hours_and_coverage_match_a_naive_recount(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_preference_usage(days=30, now=now)
        self.assertEqual(
            result['quiet_hours']['on'],
            NotificationPreference.objects.filter(quiet_hours_enabled=True).count(),
        )
        self.assertEqual(
            result['quiet_hours']['off'],
            NotificationPreference.objects.filter(quiet_hours_enabled=False).count(),
        )
        self.assertEqual(
            result['coverage']['rows_total'],
            NotificationPreference.objects.count(),
        )
        self.assertEqual(
            result['coverage']['users_with_notification'],
            Notification.objects.values('recipient').distinct().count(),
        )

    def test_group_totals_are_the_sum_of_their_member_rows(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_preference_usage(days=30, now=now)
        by_field = {row['field']: row for row in result['rows']}
        for group in result['by_group']:
            members = [by_field[field] for field in group['fields']]
            self.assertEqual(group['on'], sum(r['on'] for r in members), group['key'])
            self.assertEqual(group['off'], sum(r['off'] for r in members), group['key'])
            expected_share = (
                group['off'] / (group['on'] + group['off'])
                if (group['on'] + group['off']) else None
            )
            self.assertEqual(group['share'], expected_share, group['key'])


class PreferenceBypassOracleTests(TestCase):
    """第二件：bypass 的四个数，和一个字段一次查询的笨实现相同。"""

    def test_rows_match_a_naive_per_field_recount(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_preference_bypass_diagnosis(days=30, now=now)
        by_field = {row['field']: row for row in result['rows']}
        start = now - timedelta(days=30)

        for field in _preference_fields():
            preference_field = 'recipient__notification_preference__%s' % field
            recent = Notification.objects.filter(kind=field).annotate(
                occurred_at=Coalesce('last_occurred_at', 'created_at'),
                preference_updated_at=F('recipient__notification_preference__updated_at'),
            ).filter(
                occurred_at__gte=start, occurred_at__lte=now,
                **{preference_field: False},
            )
            suspicious = recent.filter(occurred_at__gt=F('preference_updated_at'))
            row = by_field[field]
            self.assertEqual(row['seen_rows'], recent.count(), field)
            self.assertEqual(
                row['seen_users'], recent.values('recipient').distinct().count(), field)
            self.assertEqual(row['suspicious_rows'], suspicious.count(), field)
            self.assertEqual(
                row['suspicious_users'],
                suspicious.values('recipient').distinct().count(), field,
            )
            self.assertEqual(
                row['off_users'],
                NotificationPreference.objects.filter(**{field: False}).count(), field)
            self.assertEqual(
                row['consistent_users'],
                max(0, row['seen_users'] - row['suspicious_users']), field)
            self.assertEqual(row['has_suspicious'], row['suspicious_users'] > 0, field)

    def test_totals_are_the_sum_of_their_member_rows(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_preference_bypass_diagnosis(days=30, now=now)
        self.assertEqual(
            result['totals']['off_seen_users'],
            sum(r['seen_users'] for r in result['rows']),
        )
        self.assertEqual(
            result['totals']['suspicious_rows'],
            sum(r['suspicious_rows'] for r in result['rows']),
        )
        self.assertEqual(
            result['totals']['suspicious_users'],
            sum(r['suspicious_users'] for r in result['rows']),
        )


class KindCoverageOracleTests(TestCase):
    """第三件：kind_coverage 的偏好列，和一个类型一次查询的笨实现相同。"""

    def test_preference_rows_match_a_naive_per_kind_recount(self):
        now = timezone.now()
        seed_panel_data(now)
        result = build_notification_kind_coverage(days=30, now=now)
        by_kind = {row['kind']: row for row in result['preference_rows']}

        start = _period_start(30, timezone.localtime(now).date())
        sent = {}
        for row in Notification.objects.filter(created_at__gte=start, created_at__lte=now):
            sent[row.kind] = sent.get(row.kind, 0) + 1

        for kind, count in sent.items():
            recipients = set(
                Notification.objects.filter(kind=kind).values_list('recipient_id', flat=True)
            )
            off = sum(
                1 for preference in NotificationPreference.objects.filter(
                    user_id__in=recipients)
                if not getattr(preference, kind, True)
            )
            row = by_kind[kind]
            self.assertEqual(row['sent_count'], count, kind)
            self.assertEqual(row['recipient_count'], len(recipients), kind)
            self.assertEqual(row['switched_off'], off, kind)
            self.assertEqual(
                row['switched_off_rate'],
                round(off / len(recipients) * 100, 1) if recipients else 0, kind,
            )


class NotificationPanelQueryBudgetTests(TestCase):
    """查询预算：谁把 aggregate 拆回逐字段循环，这里先红。"""

    def test_preference_usage_stays_within_budget(self):
        now = timezone.now()
        seed_panel_data(now)
        with CaptureQueriesContext(connection) as captured:
            build_notification_preference_usage(days=30, now=now)
        # 四条：偏好行是否存在、一次取回全部类型的「窗口内收到过」集合、一次
        # aggregate 出全部开关的四格、一次数收到过通知的用户。类型数量涨跌都不该
        # 改变这个数——这正是这次改动要守住的性质。
        self.assertLessEqual(
            len(captured), 4,
            'notification_preference_usage 用了 %d 次查询，预算 4：'
            '「窗口内收到过」是不是又回到逐类型查询了？' % len(captured),
        )

    def test_preference_usage_query_count_ignores_the_field_count(self):
        """查询数不随开关个数增长。

        上一次收敛把四格从 27x4 次 COUNT 降到一次 aggregate，但「窗口内收到过」仍
        然是每个类型一次查询——预算跟着字段数线性涨。新增一个开关就该多一次往返，
        而那个开关多半还没有任何通知。
        """
        now = timezone.now()
        seed_panel_data(now)
        fields = len(_preference_fields())
        with CaptureQueriesContext(connection) as captured:
            build_notification_preference_usage(days=30, now=now)
        self.assertNotEqual(fields, 0)
        self.assertEqual(
            len(captured), 4,
            '查询数 %d 跟着字段数 %d 走了：' % (len(captured), fields),
        )


    def test_preference_bypass_diagnosis_stays_within_budget(self):
        now = timezone.now()
        seed_panel_data(now)
        fields = len(_preference_fields())
        with CaptureQueriesContext(connection) as captured:
            build_notification_preference_bypass_diagnosis(days=30, now=now)
        # 一次 aggregate 出全部 off_users、一次出偏好行总数、一次出没有偏好行的
        # 接收人数，其余每个字段一次聚合——再没有别的往返。
        self.assertLessEqual(
            len(captured), fields + 3,
            'notification_preference_bypass_diagnosis 用了 %d 次查询，预算 %d：'
            '四个计数又拆回逐字段查询了？' % (len(captured), fields + 3),
        )

    def test_kind_coverage_stays_within_budget(self):
        now = timezone.now()
        seed_panel_data(now)
        with CaptureQueriesContext(connection) as captured:
            build_notification_kind_coverage(days=30, now=now)
        # 周期行一次、接收人分组一次、偏好行一次。类型数量涨跌都不该改变这个数。
        self.assertLessEqual(
            len(captured), 5,
            'notification_kind_coverage 用了 %d 次查询，预算 5：'
            '又在按类型逐个查接收人或偏好了？' % len(captured),
        )

class RecentlyNotifiedUsersEquivalenceTests(TestCase):
    """「窗口内收到过」的批量版必须和逐类型版给出同一批用户。

    批量版把 27 次 `kind = ?` 换成一次 `kind IN (...)`，看起来只是换了问法，但有两
    处容易悄悄走偏：`DISTINCT` 的列、和窗口的上下界。所以这里刻意用最笨的写法——一
    个类型一条查询——当基准，逐个类型比对。oracle 不用被改动的那个查询，改错了集合
    才会红。
    """

    def setUp(self):
        self.now = timezone.now()
        self.fields = _preference_fields()
        self.start = self.now - timedelta(days=30)

    def naive_by_kind(self):
        """一个类型一条查询，最笨也最不容易和实现一起错。"""
        return {
            field: set(
                Notification.objects.filter(
                    kind=field, created_at__gte=self.start,
                    created_at__lt=self.now + timedelta(seconds=60),
                ).values_list('recipient_id', flat=True).distinct()
            )
            for field in self.fields
        }

    def seed(self, plan):
        category = make_category()
        seller = make_user('eqvseed-seller')
        make_item(seller, category)
        for index, (field, offset_seconds, count) in enumerate(plan):
            user = make_user('equiv-%02d' % index)
            NotificationPreference.objects.create(user=user, **{field: False})
            for repeat in range(count):
                make_notification(
                    user, field, self.now - timedelta(seconds=offset_seconds),
                )

    def test_batch_version_matches_one_query_per_kind(self):
        self.seed([
            ('order_created', 3600, 1),
            ('message_received', 7200, 3),
            ('rating_received', 86400 * 20, 2),
        ])
        expected = self.naive_by_kind()
        actual = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        self.assertEqual(
            {field: actual.get(field, set()) for field in self.fields},
            expected,
        )

    def test_a_user_with_several_notifications_counts_once(self):
        """同一用户同一类型多条通知只算一个人。

        `notified_users` 是「有多少人」，不是「发了几条」。批量版少了 DISTINCT 的
        语义就会把它变成条数，而面板上这一格会无声无息地翻倍。
        """
        self.seed([('order_created', 3600, 5)])
        actual = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        self.assertEqual(actual['order_created'], {
            user.id for user in User.objects.filter(username__startswith='equiv-')
        })

    def test_the_upper_bound_keeps_the_sixty_second_margin(self):
        """上界取 `end + 60 秒`：刚写入的行不能被窗口切掉。

        `Notification.created_at` 是 `auto_now_add`，而视图先取 `now`、通知后写，两者
        间隔可以超过一秒。上界严严地等于 `now` 会把刚写入的行整批排除，那看起来就像
        一个计算错误。余量给够了，但不能无限给——超过 60 秒的行仍然必须排除。
        """
        self.seed([('order_created', -30, 1)])
        just_after = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        self.assertEqual(len(just_after['order_created']), 1)

        Notification.objects.all().delete()
        make_notification(
            make_user('equiv-late'), 'order_created',
            self.now + timedelta(seconds=120),
        )
        beyond_margin = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        self.assertEqual(beyond_margin['order_created'], set())

    def test_the_lower_bound_excludes_rows_before_the_window(self):
        """窗口之外的行不进分子，也不进分母。"""
        self.seed([('order_created', 86400 * 40, 1)])
        actual = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        self.assertEqual(actual['order_created'], set())

    def test_kinds_without_notifications_get_an_empty_set(self):
        """没收过通知的类型给空集合，不是缺键。

        调用方直接 `notified[field]`，缺键会 KeyError；而空集合会让面板走「没有偏好
        行的用户单列」那条早退分支。两件事都不能省。
        """
        self.seed([('order_created', 3600, 1)])
        actual = _recently_notified_users_by_kind(
            self.fields, self.start, self.now,
        )
        for field in self.fields:
            self.assertIn(field, actual)
        empty = [f for f in self.fields if f != 'order_created']
        self.assertTrue(empty)
        for field in empty:
            self.assertEqual(actual[field], set())

    def test_panel_numbers_agree_with_the_naive_recount(self):
        """面板上每个开关的 `notified_users` 等于逐类型重算的集合大小。"""
        self.seed([
            ('order_created', 3600, 2),
            ('message_received', 7200, 1),
            ('rating_received', 86400 * 10, 4),
        ])
        report = build_notification_preference_usage(days=30, now=self.now)
        expected = self.naive_by_kind()
        for row in report['rows']:
            self.assertEqual(
                row['notified_users'], len(expected[row['field']]),
                row['field'],
            )
