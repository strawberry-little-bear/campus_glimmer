from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .lifecycle_reminder_effect import (
    EFFECT_WINDOW_DAYS,
    NOTICE_MATCH_TOLERANCE,
    UNTOUCHED_LIMIT,
    build_lifecycle_reminder_effect,
)
from .lifecycle_reminders import send_lifecycle_reminders
from .models import (
    Category,
    CampusLocation,
    Item,
    Notification,
    NotificationPreference,
    Order,
    OrderEvent,
)


class LifecycleReminderEffectTests(TestCase):
    """Measure whether lifecycle reminders changed seller behaviour.

    The tests deliberately exercise the whole loop rather than calling the
    module in isolation: a reminder is sent through the real sender, then the
    effect view is built from the resulting rows. That way the aggregation is
    checked against the same timestamps production writes.
    """

    def setUp(self):
        self.seller = User.objects.create_user(username='effect-seller', password='safe-password-123')
        self.other = User.objects.create_user(username='effect-seller-2', password='safe-password-123')
        self.buyer = User.objects.create_user(username='effect-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='效果分类')
        self.location = CampusLocation.objects.create(name='北门')
        self.now = timezone.now()

    def make_item(self, seller=None, **kwargs):
        defaults = {
            'title': '效果商品',
            'description': '用于测试提醒效果统计',
            'price': '35.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': seller or self.seller,
        }
        age_days = kwargs.pop('age_days', None)
        defaults.update(kwargs)
        item = Item.objects.create(**defaults)
        if age_days:
            Item.objects.filter(pk=item.pk).update(
                created_at=timezone.now() - timedelta(days=age_days),
            )
            item.refresh_from_db()
        return item

    def stamp_reminder(self, item, *, attention=True, price=False, days_ago=1):
        """Pin the cool-down timestamps the real sender would have written."""
        stamp = timezone.now() - timedelta(days=days_ago)
        updates = {}
        if attention:
            updates['attention_reminder_sent_at'] = stamp
        if price:
            updates['price_drop_reminder_sent_at'] = stamp
        Item.objects.filter(pk=item.pk).update(**updates)
        item.refresh_from_db()
        return stamp

    def make_notice(self, seller=None, *, read=False, days_ago=1):
        """Create the notice the sender would have left behind."""
        notification = Notification.objects.create(
            recipient=seller or self.seller,
            kind='lifecycle_reminder',
            title='商品需要你处理',
            message='你有 1 件商品需要处理。',
            is_read=read,
        )
        created_at = timezone.now() - timedelta(days=days_ago)
        updates = {'created_at': created_at}
        if read:
            updates['read_at'] = created_at
        Notification.objects.filter(pk=notification.pk).update(**updates)
        notification.refresh_from_db()
        return notification

    def complete_order(self, item, *, days_ago=2):
        """Give the listing a terminal order event, as a real sale would."""
        order = Order.objects.create(
            item=item, buyer=self.buyer, seller=item.seller,
            agreed_price=item.price, status='completed',
        )
        OrderEvent.objects.create(
            order=order, to_status='completed',
        )
        OrderEvent.objects.filter(order=order).update(
            created_at=timezone.now() - timedelta(days=days_ago),
        )
        return order

    def data(self, days=30, now=None):
        return build_lifecycle_reminder_effect(days, now=now or self.now)

    def test_no_reminders_reports_empty_state(self):
        data = self.data()

        self.assertFalse(data['has_data'])
        self.assertEqual(data['summary']['reminded_items'], 0)
        self.assertEqual(data['kind_rows'], [])
        self.assertEqual(data['untouched_rows'], [])
        self.assertEqual(data['blocked_rows'], [])
        self.assertEqual(data['recommendations'], [])

    def test_reminded_listing_without_action_is_counted_untouched(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)
        self.make_notice(read=True)

        # 真实发送用的是当前时刻，统计终点也必须用当前时刻，否则通知会落在
        # 统计窗口之外，这里测的就不再是同一条链路了。
        data = build_lifecycle_reminder_effect(30)
        summary = data['summary']

        self.assertTrue(data['has_data'])
        self.assertEqual(summary['reminded_items'], 1)
        self.assertEqual(summary['read_items'], 1)
        self.assertEqual(summary['acted_after'], 0)
        self.assertEqual(summary['untouched'], 1)
        self.assertEqual(summary['read_but_inactive'], 1)
        self.assertEqual(summary['action_rate'], 0)
        self.assertEqual(data['untouched_total'], 1)

    def test_refresh_after_reminder_counts_as_action(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item, days_ago=3)
        self.make_notice(read=True, days_ago=3)
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )

        summary = self.data()['summary']

        self.assertEqual(summary['refreshed_after'], 1)
        self.assertEqual(summary['acted_after'], 1)
        self.assertEqual(summary['action_rate'], 100.0)
        self.assertEqual(summary['untouched'], 0)

    def test_refresh_before_reminder_is_not_a_reaction(self):
        """提醒之前就刷新过的商品不能算成提醒的功劳。"""
        item = self.make_item(age_days=60)
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=timezone.now() - timedelta(days=10), refresh_count=1,
        )
        self.stamp_reminder(item, days_ago=3)

        summary = self.data()['summary']

        self.assertEqual(summary['refreshed_after'], 0)
        self.assertEqual(summary['acted_after'], 0)

    def test_completed_order_after_reminder_counts_as_action(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item, days_ago=3)
        self.make_notice(read=True, days_ago=3)
        self.complete_order(item, days_ago=1)

        summary = self.data()['summary']

        self.assertEqual(summary['completed_after'], 1)
        self.assertEqual(summary['acted_after'], 1)

    def test_order_completed_before_reminder_is_not_a_reaction(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item, days_ago=1)
        self.make_notice(read=True, days_ago=1)
        self.complete_order(item, days_ago=5)

        summary = self.data()['summary']

        self.assertEqual(summary['completed_after'], 0)
        self.assertEqual(summary['acted_after'], 0)

    def test_reminder_outside_period_is_excluded(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item, days_ago=40)
        self.make_notice(days_ago=40)

        data = self.data(days=30)

        self.assertFalse(data['has_data'])
        self.assertEqual(data['summary']['reminded_items'], 0)

    def test_unread_notice_is_not_counted_as_read(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)
        self.make_notice(read=False)

        summary = self.data()['summary']

        self.assertEqual(summary['reached_items'], 1)
        self.assertEqual(summary['read_items'], 0)
        self.assertEqual(summary['notice_read_rate'], 0)
        # 没读过又没有动作，应该出现在“看完没动”之外的第一类里。
        self.assertEqual(summary['untouched'], 1)
        self.assertEqual(summary['read_but_inactive'], 0)

    def test_notice_without_matching_reminder_timestamp_is_not_reached(self):
        """时间差超过容差时不认为是同一次触达，避免错配。"""
        item = self.make_item(age_days=60)
        self.stamp_reminder(item, days_ago=3)
        # 通知提前了 3 小时，超过 5 分钟容差，属于另一次发送。
        # 通知比提醒早 3 小时，超过 5 分钟容差，应判为另一次发送。
        Notification.objects.filter(pk=self.make_notice(read=True, days_ago=3).pk).update(
            created_at=timezone.now() - timedelta(days=3, hours=3),
        )

        summary = self.data()['summary']

        self.assertEqual(summary['reached_items'], 0)
        self.assertEqual(summary['read_items'], 0)
        self.assertTrue(any('找不到对应的提醒记录' in row for row in self.data()['recommendations']))

    def test_price_and_attention_kinds_are_reported_separately(self):
        attention_item = self.make_item(title='仅待处理', age_days=60)
        self.stamp_reminder(attention_item, attention=True, price=False, days_ago=3)
        price_item = self.make_item(title='仅价格', age_days=60)
        self.stamp_reminder(price_item, attention=False, price=True, days_ago=3)
        both_item = self.make_item(title='两类都有', age_days=60)
        self.stamp_reminder(both_item, attention=True, price=True, days_ago=3)

        rows = {row['key']: row for row in self.data()['kind_rows']}

        # 三个桶互斥：同时收到两类的商品只进 both，否则“带证据是否更有效”
        # 就答不了了——同一个动作无法归因给任何一类。
        self.assertEqual(rows['attention']['items'], 1)
        self.assertEqual(rows['price']['items'], 1)
        self.assertEqual(rows['both']['items'], 1)
        self.assertEqual(sum(row['items'] for row in rows.values()), 3)
        self.assertEqual(rows['attention']['label'], '仅待处理提醒')
        self.assertTrue(rows['both']['note'])

    def test_untouched_rows_are_capped_but_total_is_exact(self):
        for index in range(UNTOUCHED_LIMIT + 3):
            item = self.make_item(title='闲置商品 ' + str(index), age_days=60)
            self.stamp_reminder(item, days_ago=2)

        data = self.data()

        self.assertEqual(len(data['untouched_rows']), UNTOUCHED_LIMIT)
        self.assertEqual(data['untouched_total'], UNTOUCHED_LIMIT + 3)
        # 列表上限只是展示限制，总量必须如实反映。
        self.assertEqual(data['summary']['untouched'], UNTOUCHED_LIMIT + 3)

    def test_untouched_rows_surface_the_listing_context(self):
        item = self.make_item(title='无人问津', age_days=60)
        self.stamp_reminder(item, days_ago=2)

        row = self.data()['untouched_rows'][0]

        self.assertEqual(row['item'].pk, item.pk)
        self.assertEqual(row['seller'].pk, self.seller.pk)
        self.assertEqual(row['kinds'], ['attention'])
        self.assertFalse(row['read'])
        self.assertGreaterEqual(row['days_since_reminder'], 2)
        self.assertEqual(row['age_days'], item.freshness_age_days)

    def test_closed_preference_is_reported_as_blocked(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)
        preference, _ = NotificationPreference.objects.get_or_create(user=self.seller)
        preference.lifecycle_reminder = False
        preference.save()

        rows = {row['key']: row for row in self.data()['blocked_rows']}

        self.assertEqual(rows['preference']['items'], 1)
        self.assertEqual(rows['preference']['label'], '关闭了生命周期提醒')

    def test_blocked_rows_are_empty_without_reminders(self):
        self.assertEqual(self.data()['blocked_rows'], [])

    def test_end_to_end_after_a_real_send(self):
        """走一遍真实发送，确认聚合读到的就是生产写入的时间戳。"""
        item = self.make_item(age_days=60)
        result = send_lifecycle_reminders()

        self.assertEqual(result['sent'], 1)

        data = build_lifecycle_reminder_effect(30)
        summary = data['summary']

        self.assertEqual(summary['reminded_items'], 1)
        self.assertEqual(summary['reminded_sellers'], 1)
        self.assertEqual(summary['notices'], 1)
        self.assertEqual(summary['reached_items'], 1)
        self.assertEqual(summary['read_items'], 0)
        self.assertEqual(summary['acted_after'], 0)
        self.assertEqual(summary['untouched'], 1)
        self.assertEqual(data['untouched_total'], 1)

        # 之后卖家刷新商品，同一份数据应该立刻反映出动作。
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )
        after = build_lifecycle_reminder_effect(30)
        self.assertEqual(after['summary']['refreshed_after'], 1)
        self.assertEqual(after['summary']['action_rate'], 100.0)
        self.assertEqual(after['untouched_total'], 0)

    def test_two_sellers_are_aggregated_separately(self):
        mine = self.make_item(age_days=60)
        theirs = self.make_item(seller=self.other, age_days=60)
        self.stamp_reminder(mine, days_ago=3)
        self.stamp_reminder(theirs, days_ago=3)

        summary = self.data()['summary']

        self.assertEqual(summary['reminded_items'], 2)
        self.assertEqual(summary['reminded_sellers'], 2)

    def test_only_lifecycle_notices_are_counted(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)
        # 其他类型的通知不应影响生命周期提醒的触达统计。
        Notification.objects.create(
            recipient=self.seller, kind='order_status',
            title='订单更新', message='与生命周期提醒无关。',
        )

        summary = self.data()['summary']

        self.assertEqual(summary['notices'], 0)
        self.assertEqual(summary['reached_items'], 0)

    def test_window_and_tolerance_constants_stay_within_the_documented_range(self):
        self.assertLessEqual(EFFECT_WINDOW_DAYS, 14)
        self.assertEqual(NOTICE_MATCH_TOLERANCE.total_seconds(), 5 * 60)
        self.assertGreater(UNTOUCHED_LIMIT, 0)

    def test_period_days_is_echoed_back(self):
        self.assertEqual(self.data(days=7)['period_days'], 7)

    def test_read_rate_is_rounded_to_one_decimal(self):
        for index in range(3):
            item = self.make_item(title='批量商品 ' + str(index), age_days=60)
            self.stamp_reminder(item)
            self.make_notice(read=(index == 0))

        summary = self.data()['summary']

        self.assertEqual(summary['read_items'], 1)
        self.assertEqual(summary['notice_read_rate'], 33.3)
        self.assertEqual(summary['read_rate'], 33.3)

