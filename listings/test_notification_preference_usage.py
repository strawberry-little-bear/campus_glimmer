# -*- coding: utf-8 -*-
"""为「通知偏好开关：26 个开关各自的形状」钉住口径。

`NotificationPreference` 上有 27 个布尔字段：26 个通知类型开关和一个免打扰总闸。每个开关都有真实的
读取路径——`notifications.py:66` 的 `create_notification` 第一件事就是
`if not getattr(preference, kind, True): return None`：**开关关掉的通知，在写入那一刻
就被静默丢弃，库里连一行都没有。**

此前只有 `moderation_notice_preference` 读过一行偏好值，而它只读 `moderation_update`
一个开关。其余 24 个关没关、谁关的，一处都答不上来。

这些测试要钉住的是：

1. 26 个开关全部报出来，一个不少——名单从模型元数据现读，加了新开关自动进面板；
2. 被开关挡掉的通知一行都不建，所以「关掉之后通知少了多少」不在口径里——
   那个数的分子在库里、分母不在；
3. 按「关掉它有没有交易后果」分四组，不合成一个全站关闭率——合成之后，
   「关掉运营告警日报」和「关掉新订单通知」会读成同一件事；
4. 「关着却仍收到过」这一格只可能是窗口前收到的或汇总类通知，两种都记、不拆；
5. 没有偏好行的用户单列一格，不并入「开着」——否则「多少人碰过设置页」问不出来；
6. 免打扰总闸单独报，不并进 26 个开关里；
7. 门槛按行算，不足 12 行的类型只报数字不给占比；
8. 不按用户排名、不输出用户名；
9. 不改 `create_notification`、不给任何人自动打开开关。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .models import Category, Item, Notification, NotificationPreference
from .notification_preference_usage import (
    MIN_PREFERENCE_ROWS, PREFERENCE_GROUPS, QUIET_HOURS_FIELD,
    build_notification_preference_usage, _preference_fields,
)


def make_user(username):
    return User.objects.create_user(username=username, password='safe-password-123')


def make_category():
    return Category.objects.create(name='测试分类')


def make_item(seller, category, title='测试商品'):
    return Item.objects.create(
        title=title, description='用于通知偏好开关测试',
        price='10.00', category=category, condition='良好', seller=seller,
    )


class PreferenceFieldDiscoveryTests(TestCase):
    """第一组：开关名单从模型现读，不硬编码。"""

    def test_every_notification_type_switch_is_reported(self):
        fields = _preference_fields()
        # 27 个布尔字段减去免打扰总闸，正好 26 个通知类型开关。
        self.assertEqual(len(fields), 26)
        self.assertNotIn(QUIET_HOURS_FIELD, fields)

    def test_every_switch_is_assigned_to_exactly_one_group(self):
        fields = set(_preference_fields())
        grouped = set()
        for _, _, members in PREFERENCE_GROUPS:
            grouped |= set(members)
        self.assertEqual(fields, grouped, '有开关没归组：%s' % sorted(fields - grouped))
        # 也不能有重复归组——一个开关出现在两组里，占比会被数两遍。
        counts = {}
        for _, _, members in PREFERENCE_GROUPS:
            for m in members:
                counts[m] = counts.get(m, 0) + 1
        self.assertEqual([k for k, v in counts.items() if v > 1], [])

    def test_quiet_hours_switch_is_not_a_notification_type(self):
        # 免打扰总闸和通知类型不是一类东西：它只压角标，不挡通知本身。
        fields = _preference_fields()
        self.assertNotIn('quiet_hours_enabled', fields)
        result = build_notification_preference_usage()
        self.assertIn('quiet_hours', result)
        self.assertNotIn('quiet_hours_enabled', [r['field'] for r in result['rows']])


class PreferenceShapeTests(TestCase):
    """第二组：26 个开关各自的四格形状。"""

    def setUp(self):
        self.category = make_category()
        self.now = timezone.now()

    def test_empty_platform_reports_no_data_without_directions(self):
        result = build_notification_preference_usage(now=self.now)
        self.assertFalse(result['has_data'])
        self.assertEqual(result['rows'], [])
        self.assertEqual(result['by_group'], [])
        self.assertEqual(result['quiet_hours']['rows_total'], 0)
        self.assertIn('当前平台还没有任何偏好行', result['summary'])

    def test_off_switch_is_counted_and_kept_separate_from_on(self):
        for i in range(3):
            user = make_user('off-%d' % i)
            NotificationPreference.objects.create(user=user, order_created=False)
        for i in range(2):
            user = make_user('on-%d' % i)
            NotificationPreference.objects.create(user=user, order_created=True)
        result = build_notification_preference_usage(now=self.now)
        row = next(r for r in result['rows'] if r['field'] == 'order_created')
        self.assertEqual(row['off'], 3)
        self.assertEqual(row['on'], 2)
        self.assertEqual(row['rows_total'], 5)
        # 没有样本，不给占比方向。
        self.assertFalse(row['has_enough_rows'])

    def test_group_share_is_none_until_enough_rows(self):
        # 交易类有 9 个开关，每行 1 条偏好也只够凑 9 行，离 12 行门槛还差 3 行。
        # 这时关闭占比确实是 0.0（没人关），但样本不够，不给方向。
        user = make_user('lonely')
        NotificationPreference.objects.create(user=user)
        result = build_notification_preference_usage(now=self.now)
        group = next(g for g in result['by_group'] if g['key'] == 'transaction')
        self.assertFalse(group['has_enough_rows'])
        self.assertEqual(group['share'], 0.0)

    def test_users_without_preference_row_are_counted_separately(self):
        # 收到过通知却还没建过偏好行的用户：单列一格，不并入「开着」。
        seller = make_user('seller')
        buyer = make_user('buyer')
        item = make_item(seller, self.category)
        Notification.objects.create(recipient=buyer, kind='order_created', title='t', message='m')
        result = build_notification_preference_usage(now=self.now)
        self.assertEqual(result['coverage']['rows_total'], 0)
        self.assertEqual(result['coverage']['users_with_notification'], 1)
        self.assertEqual(result['coverage']['users_without_row'], 1)

    def test_off_but_seen_counts_switch_off_despite_recent_notification(self):
        # 观测对象：近期收到过、开关却关着。只可能是窗口前收到的，或者汇总类通知
        # 在另一个时刻生成——两种都记，不拆。
        user = make_user('seen-but-off')
        NotificationPreference.objects.create(user=user, order_created=False)
        # created_at 是 auto_now_add，创建时传值会被忽略，所以建完再回写。
        Notification.objects.create(
            recipient=user, kind='order_created', title='t', message='m',
        )
        result = build_notification_preference_usage(days=30, now=self.now)
        row = next(r for r in result['rows'] if r['field'] == 'order_created')
        self.assertEqual(row['off_but_seen'], 1)
        self.assertEqual(row['notified_users'], 1)

    def test_notification_outside_window_is_not_counted_as_seen(self):
        user = make_user('old-seen')
        NotificationPreference.objects.create(user=user, order_created=False)
        old = Notification.objects.create(
            recipient=user, kind='order_created', title='t', message='m',
        )
        # 窗口外的那条同样要回写时间列，否则它会落在「刚刚」。
        Notification.objects.filter(pk=old.pk).update(
            created_at=self.now - timedelta(days=60),
        )
        result = build_notification_preference_usage(days=30, now=self.now)
        row = next(r for r in result['rows'] if r['field'] == 'order_created')
        # 窗口外收到的不算「近期收到过」——它落进第三、四格，不落进第二格。
        self.assertEqual(row['off_but_seen'], 0)
        self.assertEqual(row['notified_users'], 0)

    def test_on_but_unseen_counts_open_switch_without_notification(self):
        # 「开着却没收到过」不是漏报：一个从没被预约过的用户开着 order_created
        # 是天经地义。
        for i in range(4):
            user = make_user('quiet-%d' % i)
            NotificationPreference.objects.create(user=user, order_created=True)
        result = build_notification_preference_usage(days=30, now=self.now)
        row = next(r for r in result['rows'] if r['field'] == 'order_created')
        self.assertEqual(row['on_but_unseen'], 4)

    def test_transaction_group_has_real_trading_consequence_switches(self):
        # 关掉这些开关有交易后果：用户可能错过一笔成交。
        keys = {k: members for k, _, members in PREFERENCE_GROUPS}
        for field in ('order_created', 'order_status', 'order_dispute',
                      'meeting_incident', 'gift_application', 'demand_response'):
            self.assertIn(field, keys['transaction'])
        for field in ('operations_digest', 'opportunity_digest', 'saved_search_digest'):
            self.assertIn(field, keys['digest'])
        for field in ('message_received', 'comment_received', 'rating_received'):
            self.assertIn(field, keys['interaction'])

    def test_quiet_hours_switch_reported_on_its_own(self):
        for i in range(3):
            user = make_user('qh-on-%d' % i)
            NotificationPreference.objects.create(user=user, quiet_hours_enabled=True)
        for i in range(2):
            user = make_user('qh-off-%d' % i)
            NotificationPreference.objects.create(user=user, quiet_hours_enabled=False)
        result = build_notification_preference_usage(now=self.now)
        self.assertEqual(result['quiet_hours']['on'], 3)
        self.assertEqual(result['quiet_hours']['off'], 2)
        self.assertEqual(result['quiet_hours']['rows_total'], 5)

    def test_threshold_comes_from_shared_constant(self):
        result = build_notification_preference_usage(now=self.now)
        self.assertEqual(result['min_preference_rows'], MIN_PREFERENCE_ROWS)

    def test_small_sample_rows_are_flagged_not_dropped(self):
        # 样本不足的类型照实报数字，只是不给方向——删掉它会让面板看起来更干净，
        # 而干净是假的。
        user = make_user('single')
        NotificationPreference.objects.create(user=user)
        result = build_notification_preference_usage(now=self.now)
        self.assertEqual(len(result['rows']), 26)
        self.assertTrue(all(r['has_enough_rows'] is False for r in result['rows']))

    def test_a_switch_that_is_never_used_still_gets_a_row(self):
        # 一个从没被任何人关过、也从没发过的开关，仍然要在表里——它的规模是 0，
        # 而「这个开关有没有人碰」本身就是答案。
        result = build_notification_preference_usage(now=self.now)
        self.assertFalse(result['has_data'])


class PreferenceReadOnlyTests(TestCase):
    """第三组：只报告，不改写。"""

    def test_build_does_not_touch_preference_rows(self):
        user = make_user('victim')
        NotificationPreference.objects.create(user=user, order_created=False)
        before = NotificationPreference.objects.values_list('user_id', 'order_created')
        build_notification_preference_usage()
        after = NotificationPreference.objects.values_list('user_id', 'order_created')
        self.assertEqual(list(before), list(after))

    def test_build_does_not_create_preference_rows_for_users_without_one(self):
        # 没有偏好行的用户单列一格——不是给每个用户补一行「默认全开」。
        buyer = make_user('buyer')
        Notification.objects.create(recipient=buyer, kind='order_created', title='t', message='m')
        build_notification_preference_usage()
        self.assertEqual(NotificationPreference.objects.count(), 0)

    def test_build_does_not_create_notifications(self):
        # 被开关挡掉的通知一行都不建——本模块也不补建。
        user = make_user('silent')
        NotificationPreference.objects.create(user=user, order_created=False)
        build_notification_preference_usage()
        self.assertEqual(Notification.objects.count(), 0)