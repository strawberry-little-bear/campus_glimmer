from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from .lifecycle_reminder_action_trend import (
    MIN_PERIOD_ITEMS,
    TREND_DELTA_POINTS,
    build_lifecycle_reminder_action_trend,
)
from .models import (
    Category,
    CampusLocation,
    Item,
    Notification,
    NotificationPreference,
    Order,
    OrderEvent,
)


class LifecycleReminderActionTrendTests(TestCase):
    """Compare the reminder action rate with the period before it.

    The tests build both windows out of the same primitives the single-period
    panel uses, then assert on what the comparison is and is not allowed to
    say. Four things are pinned: the window cut, the sample floor, the `both`
    bucket having no direction, and a period with no reminders not being
    reported as zero.
    """

    def setUp(self):
        self.seller = User.objects.create_user(username='trend-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='trend-buyer', password='safe-password-123')
        self.category = Category.objects.create(name='趋势分类')
        self.location = CampusLocation.objects.create(name='东门')

    def make_item(self, **kwargs):
        defaults = {
            'title': '趋势商品',
            'description': '用于测试提醒行动率的跨周期对比',
            'price': '35.00',
            'category': self.category,
            'location': self.location,
            'condition': '9成新',
            'seller': self.seller,
        }
        defaults.update(kwargs)
        return Item.objects.create(**defaults)

    def stamp_reminder(self, item, *, attention=True, price=False, days_ago):
        stamp = timezone.now() - timedelta(days=days_ago)
        updates = {}
        if attention:
            updates['attention_reminder_sent_at'] = stamp
        if price:
            updates['price_drop_reminder_sent_at'] = stamp
        Item.objects.filter(pk=item.pk).update(**updates)
        item.refresh_from_db()
        return stamp

    def make_notice(self, *, read=False, days_ago):
        notification = Notification.objects.create(
            recipient=self.seller,
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

    def act(self, item, *, days_ago):
        """Give the listing a refresh, the action the effect panel recognises."""
        Item.objects.filter(pk=item.pk).update(
            last_refreshed_at=timezone.now() - timedelta(days=days_ago),
            refresh_count=1,
        )

    def data(self, days=30):
        return build_lifecycle_reminder_action_trend(days)

    def test_two_empty_periods_report_no_data(self):
        data = self.data()

        self.assertFalse(data['has_data'])
        self.assertFalse(data['has_sample'])
        self.assertEqual(data['summary'], '两个周期内都没有发送过生命周期提醒，跨周期对比需要先有提醒记录。')
        self.assertEqual(data['metric_rows'][3]['current'], None)
        self.assertEqual(data['metric_rows'][3]['previous'], None)

    def test_current_window_is_measured_from_local_midnight(self):
        """A reminder inside the previous window must not land in this one.

        Both windows are cut by the same local-midnight bounds the rest of the
        dashboard uses, so a reminder counted in the previous window here is
        counted there on every other panel too. Thirty days is used so the
        boundary is unambiguous: yesterday belongs to this period, and the day
        before yesterday does too, while anything older than the window does
        not.
        """
        inside = self.make_item()
        self.stamp_reminder(inside, days_ago=1)
        self.make_notice(read=True, days_ago=1)
        self.act(inside, days_ago=0)

        data = self.data()

        self.assertEqual(data['current']['reminded_items'], 1)
        self.assertEqual(data['previous']['reminded_items'], 0)
        self.assertEqual(data['current']['action_rate'], 100.0)
        self.assertIsNone(data['previous']['action_rate'])
        self.assertEqual(data['summary'], '上一周期没有提醒记录，本期是第一个有提醒的周期，暂时没有可比的基数。')

    def test_reminder_older_than_window_falls_in_previous_period(self):
        old = self.make_item()
        self.stamp_reminder(old, days_ago=40)
        self.make_notice(read=True, days_ago=40)
        self.act(old, days_ago=39)

        data = self.data()

        self.assertEqual(data['current']['reminded_items'], 0)
        self.assertEqual(data['previous']['reminded_items'], 1)
        self.assertIsNone(data['current']['action_rate'])
        self.assertEqual(data['previous']['action_rate'], 100.0)
        self.assertEqual(data['summary'], '本周期还没有发送过生命周期提醒，行动率不存在，不是零。')

    def test_both_windows_reported_without_direction_below_floor(self):
        """Thin evidence is withheld, not guessed at.

        Below `MIN_PERIOD_ITEMS` the action rate moves in whole steps, so a
        difference between two thin windows is arithmetic. The figures are
        still reported - a panel that hides numbers is worse than one that
        refuses to colour them.
        """
        for index in range(MIN_PERIOD_ITEMS - 2):
            item = self.make_item(title=f'本期 {index}')
            self.stamp_reminder(item, days_ago=2)
            self.make_notice(read=True, days_ago=2)
            self.act(item, days_ago=1)
        for index in range(MIN_PERIOD_ITEMS - 2):
            item = self.make_item(title=f'上期 {index}')
            self.stamp_reminder(item, days_ago=35)
            self.make_notice(read=True, days_ago=35)
            self.act(item, days_ago=34)

        data = self.data()

        self.assertFalse(data['has_sample'])
        self.assertEqual(data['current']['reminded_items'], MIN_PERIOD_ITEMS - 2)
        self.assertEqual(data['previous']['reminded_items'], MIN_PERIOD_ITEMS - 2)
        rate_row = data['metric_rows'][3]
        self.assertEqual(rate_row['direction'], 'insufficient')
        self.assertEqual(rate_row['direction_label'], '样本不足')
        self.assertIn('不足', data['summary'])

    def test_action_rate_direction_when_both_windows_have_samples(self):
        """The one metric that carries a direction, read across two windows."""
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'本期 {index}')
            self.stamp_reminder(item, days_ago=2)
            self.make_notice(read=True, days_ago=2)
            self.act(item, days_ago=1)
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'上期 {index}')
            self.stamp_reminder(item, days_ago=35)
            self.make_notice(read=True, days_ago=35)

        data = self.data()

        self.assertTrue(data['has_sample'])
        rate_row = data['metric_rows'][3]
        self.assertEqual(rate_row['key'], 'action_rate')
        self.assertEqual(rate_row['current'], 100.0)
        self.assertEqual(rate_row['previous'], 0.0)
        self.assertEqual(rate_row['direction'], 'rising')
        self.assertEqual(rate_row['tone'], 'good')
        self.assertIn('升到', data['summary'])

    def test_only_the_action_rate_carries_a_direction(self):
        """A volume change is not a quality change.

        The reminded count and the read rate are reported with their delta but
        never given a direction word: a busier period is not a better one, and
        an opened notice is not an acted-on listing.
        """
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'本期 {index}')
            self.stamp_reminder(item, days_ago=2)
            self.make_notice(read=True, days_ago=2)
            self.act(item, days_ago=1)
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'上期 {index}')
            self.stamp_reminder(item, days_ago=35)
            self.make_notice(read=True, days_ago=35)
            self.act(item, days_ago=34)

        data = self.data()

        for row in data['metric_rows']:
            if row['key'] == 'action_rate':
                self.assertTrue(row['has_direction'])
            else:
                self.assertFalse(row['has_direction'])
                self.assertIsNone(row['direction'])
                self.assertEqual(row['direction_label'], '')

    def test_both_bucket_never_gets_a_direction(self):
        """A listing reached by both kinds cannot attribute its outcome to either.

        The single-period panel already keeps this bucket report-only, and a
        direction computed over it would read that same unattributable bucket
        as a trend.
        """
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'本期 {index}')
            self.stamp_reminder(item, attention=True, price=True, days_ago=2)
            self.make_notice(read=True, days_ago=2)
            self.act(item, days_ago=1)

        data = self.data()

        both = next(row for row in data['kind_rows'] if row['key'] == 'both')
        self.assertEqual(both['current_items'], MIN_PERIOD_ITEMS + 2)
        self.assertFalse(both['has_direction'])
        self.assertIsNone(both['direction'])
        self.assertEqual(both['direction_label'], '')
        self.assertIsNone(both['tone'])

    def test_single_kind_bucket_gets_a_direction_across_periods(self):
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'本期 {index}')
            self.stamp_reminder(item, attention=True, price=False, days_ago=2)
            self.make_notice(read=True, days_ago=2)
            self.act(item, days_ago=1)
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'上期 {index}')
            self.stamp_reminder(item, attention=True, price=False, days_ago=35)
            self.make_notice(read=True, days_ago=35)

        data = self.data()

        attention = next(row for row in data['kind_rows'] if row['key'] == 'attention')
        self.assertTrue(attention['has_direction'])
        self.assertEqual(attention['direction'], 'rising')
        self.assertEqual(attention['current_action_rate'], 100.0)
        self.assertEqual(attention['previous_action_rate'], 0.0)

    def test_action_before_reminder_is_not_counted_in_either_window(self):
        """The comparison inherits the single-period panel's direction rule.

        A refresh that predates the reminder is not a reaction to it, so a
        period whose only refreshes all happened before the nudges reports an
        action rate of zero rather than a flattering one.
        """
        for index in range(MIN_PERIOD_ITEMS + 2):
            item = self.make_item(title=f'本期 {index}')
            self.act(item, days_ago=10)
            self.stamp_reminder(item, days_ago=2)
            self.make_notice(read=True, days_ago=2)

        data = self.data()

        self.assertEqual(data['current']['reminded_items'], MIN_PERIOD_ITEMS + 2)
        self.assertEqual(data['current']['action_rate'], 0.0)

    def test_period_days_are_configurable(self):
        data = build_lifecycle_reminder_action_trend(7)

        self.assertEqual(data['days'], 7)
        self.assertEqual(data['trend_delta_points'], TREND_DELTA_POINTS)
        self.assertEqual(data['min_period_items'], MIN_PERIOD_ITEMS)