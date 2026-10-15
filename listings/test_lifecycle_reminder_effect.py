from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .lifecycle_reminder_effect import (
    EFFECT_WINDOW_DAYS,
    MIN_SEGMENT_ITEMS,
    NOTICE_MATCH_TOLERANCE,
    UNTOUCHED_LIMIT,
    build_lifecycle_reminder_effect,
)
from .lifecycle_reminders import send_lifecycle_reminders
from .models import (
    Category,
    CampusLocation,
    Item,
    LifecycleReminderEvent,
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

    def record_event(self, item, *, kind='attention', notification=None, days_ago=1):
        """Pin an item-level event row written by the real sender."""
        return LifecycleReminderEvent.objects.create(
            item=item,
            seller=item.seller,
            kind=kind,
            sent_at=timezone.now() - timedelta(days=days_ago),
            notification=notification,
        )

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
        self.assertEqual(data['category_rows'], [])
        self.assertEqual(data['seller_load_rows'], [])
        self.assertEqual(data['untouched_rows'], [])
        self.assertEqual(data['blocked_rows'], [])
        self.assertEqual(data['recommendations'], [])

    def test_event_ledger_is_preferred_over_legacy_timestamp(self):
        item = self.make_item(age_days=60)
        # 旧时间戳与另一条不匹配的通知都存在，但不能把同一次提醒拆成两份。
        self.stamp_reminder(item, days_ago=2)
        self.make_notice(read=False, days_ago=2)
        notice = self.make_notice(read=True, days_ago=1)
        self.record_event(item, kind='attention', notification=notice, days_ago=1)

        data = build_lifecycle_reminder_effect(30)
        summary = data['summary']

        self.assertEqual(summary['reminded_items'], 1)
        self.assertEqual(summary['reached_items'], 1)
        self.assertEqual(summary['read_items'], 1)
        self.assertEqual(summary['event_backed_items'], 1)
        self.assertEqual(summary['mixed_source_items'], 0)
        self.assertEqual(summary['legacy_timestamp_items'], 0)
        self.assertEqual(data['kind_rows'][0]['items'], 1)

    def test_event_and_legacy_kinds_can_form_one_mixed_item_without_double_counting(self):
        item = self.make_item(age_days=60)
        notice = self.make_notice(read=True, days_ago=1)
        self.record_event(item, kind='attention', notification=notice, days_ago=1)
        self.stamp_reminder(item, attention=True, price=True, days_ago=2)

        data = build_lifecycle_reminder_effect(30)
        summary = data['summary']

        self.assertEqual(summary['reminded_items'], 1)
        self.assertEqual(summary['event_backed_items'], 0)
        self.assertEqual(summary['mixed_source_items'], 1)
        self.assertEqual(summary['legacy_timestamp_items'], 0)
        self.assertEqual(data['kind_rows'][0]['key'], 'both')
        self.assertEqual(data['kind_rows'][0]['items'], 1)

    def test_event_remains_after_its_merged_notice_is_removed(self):
        item = self.make_item(age_days=60)
        notice = self.make_notice(read=True, days_ago=1)
        self.record_event(item, kind='attention', notification=notice, days_ago=1)
        notice.delete()

        data = build_lifecycle_reminder_effect(30)
        summary = data['summary']

        self.assertTrue(data['has_data'])
        self.assertEqual(summary['reminded_items'], 1)
        self.assertEqual(summary['event_backed_items'], 1)
        self.assertEqual(summary['reached_items'], 0)
        self.assertEqual(summary['read_items'], 0)

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



    def test_category_rows_separate_actions_and_keep_their_order(self):
        book = Category.objects.create(name='教材')
        device = Category.objects.create(name='数码')
        acted = self.make_item(title='提醒后整理的教材', category=book, age_days=60)
        stale = self.make_item(
            seller=self.other, title='提醒前整理的数码', category=device, age_days=60,
        )
        self.stamp_reminder(acted, days_ago=3)
        self.stamp_reminder(stale, days_ago=3)
        self.make_notice(read=True, days_ago=3)
        self.make_notice(seller=self.other, read=True, days_ago=3)
        Item.objects.filter(pk=acted.pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )
        Item.objects.filter(pk=stale.pk).update(
            last_refreshed_at=timezone.now() - timedelta(days=10), refresh_count=1,
        )

        rows = {row['category_name']: row for row in self.data()['category_rows']}

        self.assertEqual(rows['教材']['items'], 1)
        self.assertEqual(rows['教材']['acted_after'], 1)
        self.assertEqual(rows['教材']['action_rate'], 100.0)
        self.assertEqual(rows['数码']['refreshed_after'], 0)
        self.assertEqual(rows['数码']['acted_after'], 0)
        self.assertEqual(
            [row['category_name'] for row in self.data()['category_rows']],
            ['教材', '数码'],
        )

    def test_small_category_reports_numbers_without_direction(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)

        row = self.data()['category_rows'][0]

        self.assertEqual(row['items'], 1)
        self.assertEqual(row['action_rate'], 0)
        self.assertFalse(row['comparable'])
        self.assertEqual(row['sample_label'], '样本不足')
        self.assertLess(MIN_SEGMENT_ITEMS, 13)

    def test_seller_load_rows_use_in_period_reminded_item_counts(self):
        heavy = User.objects.create_user(
            username='effect-heavy-load', password='safe-password-123',
        )
        single = self.make_item(title='单件卖家', age_days=60)
        self.stamp_reminder(single, days_ago=2)
        few_items = [
            self.make_item(
                seller=self.other, title='少量商品 ' + str(index), age_days=60,
            )
            for index in range(2)
        ]
        many_items = [
            self.make_item(seller=heavy, title='多件商品 ' + str(index), age_days=60)
            for index in range(4)
        ]
        for item in few_items + many_items:
            self.stamp_reminder(item, days_ago=2)
        Item.objects.filter(pk=single.pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )
        Item.objects.filter(pk=few_items[0].pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )
        for item in many_items[:2]:
            Item.objects.filter(pk=item.pk).update(
                last_refreshed_at=timezone.now(), refresh_count=1,
            )

        data = self.data()
        rows = {row['key']: row for row in data['seller_load_rows']}

        self.assertEqual(rows['one']['label'], '1 件')
        self.assertEqual(rows['one']['sellers'], 1)
        self.assertEqual(rows['one']['items'], 1)
        self.assertEqual(rows['one']['acted_after'], 1)
        self.assertEqual(rows['few']['sellers'], 1)
        self.assertEqual(rows['few']['items'], 2)
        self.assertEqual(rows['few']['acted_after'], 1)
        self.assertEqual(rows['many']['sellers'], 1)
        self.assertEqual(rows['many']['items'], 4)
        self.assertEqual(rows['many']['acted_after'], 2)
        self.assertEqual(
            sum(row['items'] for row in data['seller_load_rows']),
            data['summary']['reminded_items'],
        )
        self.assertEqual(
            sum(row['acted_after'] for row in data['seller_load_rows']),
            data['summary']['acted_after'],
        )

    def test_segment_rows_do_not_expose_seller_identity(self):
        item = self.make_item(age_days=60)
        self.stamp_reminder(item)

        data = self.data()

        for row in data['category_rows'] + data['seller_load_rows']:
            self.assertNotIn('seller', row)
            self.assertNotIn('seller_id', row)
            self.assertNotIn('username', row)
            self.assertNotIn('item', row)


    def test_dashboard_and_export_render_the_two_segment_tables(self):
        category = Category.objects.create(name='分层分类')
        item = self.make_item(title='分层商品', category=category, age_days=60)
        self.stamp_reminder(item, days_ago=2)
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=timezone.now(), refresh_count=1,
        )
        staff = User.objects.create_user(
            username='effect-staff', password='safe-password-123', is_staff=True,
        )
        self.client.force_login(staff)

        response = self.client.get(reverse('operations_dashboard'), {'days': 30})

        self.assertEqual(response.status_code, 200)
        html = response.content.decode('utf-8')
        self.assertIn('生命周期提醒触达与行动', html)
        self.assertIn('按商品分类看行动率', html)
        self.assertIn('按本周期被提醒件数看行动率', html)
        self.assertIn('这是提醒负担，不是卖家库存规模', html)
        self.assertIn('不足，只报数', html)
        self.assertNotIn('effect-seller', html)

        export = self.client.get(reverse('operations_dashboard_export'), {'days': 30})

        self.assertEqual(export.status_code, 200)
        csv_text = export.content.decode('utf-8-sig')
        self.assertIn('生命周期提醒效果：按商品分类', csv_text)
        self.assertIn('生命周期提醒效果：按卖家本周期提醒负担', csv_text)
        self.assertIn('分层分类', csv_text)
        self.assertIn('1 件', csv_text)
        self.assertIn('不足，只报数', csv_text)
        self.assertNotIn('effect-seller', csv_text)
