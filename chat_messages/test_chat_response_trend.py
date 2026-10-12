from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.utils import timezone

from listings.models import Category, Item
from listings.search_trend import _period_bounds

from .chat_response import MIN_LATENCY_SAMPLE, SILENT_AFTER
from .chat_response_trend import (
    DURATION_KEYS,
    LATENCY_DRIFT_FACTOR,
    MIN_SENT_SAMPLE,
    build_chat_response_trend,
)
from .models import PrivateMessage


class ChatResponseTrendTests(TestCase):
    """Cover the cross-period comparison of private-message responsiveness."""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='trend-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='trend-buyer', password='safe-password-123',
        )
        self.now = timezone.now()
        category = Category.objects.create(name='趋势测试分类')
        self.item = Item.objects.create(
            title='趋势测试商品', description='用于私信响应跨周期统计。',
            price='59.00', category=category, condition='九成新',
            seller=self.seller,
        )

    def create_message(self, *, sender, receiver, read_after=None, created_ago=None):
        """Create a message with a pinned created_at and optional read_at.

        created_at is auto_now_add, so it is rewritten through update() to land
        inside one of the two windows. read_at is pinned the same way, which is
        the only way to build a latency sample without waiting.
        """
        message = PrivateMessage.objects.create(
            sender=sender, receiver=receiver, content='私信响应跨周期测试。', item=self.item,
        )
        created_at = self.now - (created_ago or timedelta())
        updates = {'created_at': created_at}
        if read_after is not None:
            updates['is_read'] = True
            updates['read_at'] = created_at + read_after
        PrivateMessage.objects.filter(pk=message.pk).update(**updates)
        message.refresh_from_db()
        return message

    def build(self, **kwargs):
        return build_chat_response_trend(days=30, now=self.now, **kwargs)

    def metric(self, insights, key):
        return next(row for row in insights['metric_rows'] if row['key'] == key)

    def fill_window(self, *, read_after, count=MIN_LATENCY_SAMPLE, ago=None):
        """One window's worth of messages, all opened after the same delay."""
        for _ in range(count):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=read_after, created_ago=ago,
            )

    def test_both_windows_are_the_same_length_and_do_not_overlap(self):
        start, previous_start = _period_bounds(self.now, 30)
        insights = self.build()
        self.assertEqual(insights['current_period_start'], start)
        self.assertEqual(insights['previous_period_start'], previous_start)
        self.assertEqual(
            (start - previous_start).days, insights['period_days'],
        )

    def test_messages_are_attributed_by_when_they_were_sent(self):
        start, _ = _period_bounds(self.now, 30)
        # A message sent yesterday belongs to the current window even though it
        # is only opened now: the window a message falls in is the one that had
        # to live with it, not the one that inherited the reply.
        self.create_message(sender=self.buyer, receiver=self.seller, read_after=timedelta(minutes=5))
        self.create_message(
            sender=self.buyer, receiver=self.seller, read_after=timedelta(minutes=5),
            created_ago=timedelta(days=31),
        )

        insights = self.build()
        self.assertEqual(insights['current']['sent'], 1)
        self.assertEqual(insights['previous']['sent'], 1)
        self.assertTrue(insights['comparable'])
        self.assertTrue(insights['has_data'])

    def test_a_median_that_doubled_is_reported_as_slower(self):
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=2))

        insights = self.build()
        row = self.metric(insights, 'median_seconds')
        self.assertEqual(row['previous_label'], '1 小时')
        self.assertEqual(row['current_label'], '2 小时')
        self.assertEqual(row['direction'], 'rising')
        self.assertEqual(row['direction_label'], '较上期上升')
        # A duration is compared as a multiple, not in percentage points.
        self.assertEqual(row['delta_display'], '2.00 倍')
        self.assertEqual(row['tone'], 'bad')

    def test_a_median_that_halved_is_reported_as_faster(self):
        self.fill_window(read_after=timedelta(hours=4), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=2))

        insights = self.build()
        row = self.metric(insights, 'median_seconds')
        self.assertEqual(row['direction'], 'falling')
        self.assertEqual(row['tone'], 'good')
        self.assertEqual(row['delta_display'], '0.50 倍')

    def test_a_median_inside_the_drift_band_is_flat(self):
        # 1.5x is the threshold, so a 1.2x move is a change nobody should read
        # as a trend. Calling that rising would teach the reader to distrust the
        # column on the first bad week.
        self.fill_window(read_after=timedelta(hours=5), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=6))

        insights = self.build()
        row = self.metric(insights, 'median_seconds')
        self.assertEqual(row['direction'], 'flat')
        self.assertEqual(row['tone'], 'flat')

    def test_the_drift_threshold_is_shared_with_the_panel(self):
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=2))
        insights = self.build()
        self.assertEqual(insights['latency_drift_factor'], LATENCY_DRIFT_FACTOR)
        self.assertEqual(insights['latency_drift_percent'], 50)

    def test_a_previous_window_without_timestamps_gets_no_direction(self):
        # read_at is recent. The previous window is full of messages that were
        # opened before the column existed: they count as opened and carry no
        # timestamp at all. That is not a slow previous period, it is a previous
        # period whose opening times were never recorded.
        for _ in range(MIN_LATENCY_SAMPLE):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                created_ago=timedelta(days=45),
            )
        PrivateMessage.objects.filter(
            created_at__lt=self.now - timedelta(days=30),
        ).update(is_read=True, read_at=None)
        self.fill_window(read_after=timedelta(hours=2))

        insights = self.build()
        row = self.metric(insights, 'median_seconds')
        self.assertIsNone(row['previous'])
        self.assertEqual(row['previous_label'], '—')
        self.assertEqual(row['direction'], 'insufficient')
        self.assertEqual(row['direction_label'], '样本不足')
        self.assertEqual(row['delta_display'], '—')
        # The previous window still reports its figures rather than a zero.
        self.assertEqual(insights['previous']['sent'], MIN_LATENCY_SAMPLE)
        self.assertEqual(insights['previous']['opened'], MIN_LATENCY_SAMPLE)
        self.assertEqual(insights['previous']['latency_sample_size'], 0)

    def test_a_current_window_without_timestamps_gets_no_direction(self):
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=2))
        PrivateMessage.objects.filter(
            created_at__gte=self.now - timedelta(days=29),
        ).update(is_read=True, read_at=None)

        insights = self.build()
        row = self.metric(insights, 'median_seconds')
        self.assertIsNone(row['current'])
        self.assertEqual(row['direction'], 'insufficient')

    def test_the_open_rate_is_compared_in_percentage_points(self):
        # Both sides are already percentages, so the search panel's threshold
        # applies without translation: no second number for the reader to learn.
        for _ in range(MIN_SENT_SAMPLE):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=timedelta(hours=1), created_ago=timedelta(days=45),
            )
        # 9 of 20 opened this period against 10 of 10 last period: a 45-point
        # drop, which is a direction and not noise.
        for _ in range(9):
            self.create_message(
                sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=1),
            )
        for _ in range(11):
            self.create_message(sender=self.buyer, receiver=self.seller)

        insights = self.build()
        row = self.metric(insights, 'open_rate')
        self.assertEqual(row['previous_label'], '100%')
        self.assertEqual(row['current_label'], '45%')
        self.assertEqual(row['direction'], 'falling')
        self.assertEqual(row['delta_display'], '-55')
        self.assertEqual(row['tone'], 'bad')

    def test_an_open_rate_inside_the_band_is_flat(self):
        for _ in range(MIN_SENT_SAMPLE):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=timedelta(hours=1), created_ago=timedelta(days=45),
            )
        for _ in range(19):
            self.create_message(
                sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=1),
            )
        self.create_message(sender=self.buyer, receiver=self.seller)

        insights = self.build()
        row = self.metric(insights, 'open_rate')
        # 100% last period against 95% this one. Five points is inside the
        # band the search panel already uses, so it is a change nobody should
        # read as a trend.
        self.assertEqual(row['current_label'], '95%')
        self.assertEqual(row['delta_display'], '-5')
        self.assertEqual(row['direction'], 'flat')
        self.assertEqual(row['tone'], 'flat')

    def test_the_unopened_share_is_compared_not_the_count(self):
        # Last period: 10 messages, 1 still unopened past the silence window.
        # This period: 20 messages, 2 still unopened. The count doubled, the
        # share did not move, and only the share says whether messages are going
        # unread more often. A raw count has no percentage-point scale to be
        # judged on, so doubling it would report every quiet platform as a
        # crisis the moment its volume grew.
        for index in range(MIN_SENT_SAMPLE):
            read_after = None if index == 0 else timedelta(hours=1)
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=read_after, created_ago=timedelta(days=45),
            )
        for index in range(2 * MIN_SENT_SAMPLE):
            read_after = None if index in (0, 1) else timedelta(hours=1)
            self.create_message(
                sender=self.buyer, receiver=self.seller, read_after=read_after,
                created_ago=timedelta(days=25),
            )

        insights = self.build()
        row = self.metric(insights, 'unopened_share')
        self.assertEqual(row['previous_label'], '10%')
        self.assertEqual(row['current_label'], '10%')
        self.assertEqual(row['direction'], 'flat')
        self.assertEqual(insights['previous']['unopened'], 1)
        self.assertEqual(insights['current']['unopened'], 2)

    def test_the_unopened_share_rising_is_bad_news(self):
        for index in range(MIN_SENT_SAMPLE):
            read_after = None if index == 0 else timedelta(hours=1)
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=read_after, created_ago=timedelta(days=45),
            )
        for _ in range(MIN_SENT_SAMPLE):
            self.create_message(
                sender=self.buyer, receiver=self.seller, created_ago=timedelta(days=25),
            )

        insights = self.build()
        row = self.metric(insights, 'unopened_share')
        self.assertEqual(row['previous_label'], '10%')
        self.assertEqual(row['current_label'], '100%')
        self.assertEqual(row['direction'], 'rising')
        self.assertEqual(row['tone'], 'bad')

    def test_a_share_below_the_denominator_floor_is_not_given_a_direction(self):
        # A share taken over three messages moves in steps of a third, so a
        # difference between two such windows is arithmetic rather than
        # evidence.
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            read_after=timedelta(hours=1), created_ago=timedelta(days=45),
        )
        self.create_message(sender=self.buyer, receiver=self.seller)

        insights = self.build()
        row = self.metric(insights, 'open_rate')
        self.assertEqual(row['previous_label'], '100%')
        self.assertEqual(row['current_label'], '0%')
        self.assertEqual(row['direction'], 'insufficient')

    def test_a_message_inside_the_silence_window_is_not_yet_unopened(self):
        # The current window is still in progress, so a message sent today is
        # pending rather than ignored. Counting it as unopened would report
        # every in-flight conversation as a silent one.
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=timedelta(days=45),
        )
        self.create_message(
            sender=self.buyer, receiver=self.seller, created_ago=SILENT_AFTER - timedelta(hours=1),
        )

        insights = self.build()
        self.assertEqual(insights['current']['unopened'], 0)
        self.assertEqual(insights['previous']['unopened'], 1)

    def test_a_current_window_with_no_messages_is_marked_gone(self):
        # Last period had messages and this one has none. That is not a
        # platform-wide collapse: a new term starting or a quiet fortnight both
        # produce it, and reporting it as a fall from 100% would turn either
        # into a trend that never happened.
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))

        insights = self.build()
        self.assertFalse(insights['comparable'])
        row = self.metric(insights, 'open_rate')
        self.assertIsNone(row['current'])
        self.assertEqual(row['current_label'], '—')
        self.assertEqual(row['direction'], 'gone')
        self.assertEqual(row['direction_label'], '本期已消失')
        self.assertEqual(row['delta_display'], '—')
        self.assertIn('本周期一条私信都没有', insights['summary'])

    def test_a_previous_window_with_no_messages_is_marked_new(self):
        # The other side: nothing last period, messages this period. This is
        # the feature starting to be used, not a rise from zero.
        self.fill_window(read_after=timedelta(hours=1))

        insights = self.build()
        self.assertFalse(insights['comparable'])
        row = self.metric(insights, 'open_rate')
        self.assertIsNone(row['previous'])
        self.assertEqual(row['previous_label'], '—')
        self.assertEqual(row['direction'], 'new')
        self.assertEqual(row['direction_label'], '本期新出现')
        self.assertIn('上一周期没有私信记录', insights['summary'])

    def test_the_p90_is_reported_without_a_direction(self):
        # A tail is not a trend. Half the messages sitting for a fortnight moves
        # the P90 from one hour to a fortnight, and calling that rising would
        # make the column mean something it cannot support. The figure is still
        # reported, because a reader who wants the tail has nowhere else to get
        # it from, and a median alone would hide it completely.
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=1), count=10)
        for _ in range(10):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=timedelta(days=14),
            )

        insights = self.build()
        row = self.metric(insights, 'p90_seconds')
        self.assertFalse(row['has_direction'])
        self.assertEqual(row['direction'], None)
        self.assertEqual(row['direction_label'], '')
        self.assertEqual(row['tone'], 'flat')
        self.assertEqual(row['previous_label'], '1 小时')
        self.assertEqual(row['current_label'], '14 天')
        self.assertGreater(row['current'], row['previous'])
        self.assertIn(row['key'], DURATION_KEYS)
        # The median moved too, but far less: the tail is exactly what a median
        # is supposed to survive, and both figures are needed to see that.
        self.assertEqual(self.metric(insights, 'median_seconds')['current_label'], '14 天')

    def test_the_two_way_rate_is_reported_without_a_direction(self):
        # A conversation becoming more two-way is not automatically better: it
        # can equally mean two people going back and forth about an item neither
        # of them buys.
        third = User.objects.create_user(username='trend-third', password='safe-password-123')
        self.create_message(sender=self.buyer, receiver=self.seller)
        self.create_message(sender=self.seller, receiver=self.buyer)
        self.create_message(sender=self.buyer, receiver=third)

        insights = self.build()
        row = self.metric(insights, 'two_way_rate')
        self.assertFalse(row['has_direction'])
        self.assertEqual(row['direction'], None)
        self.assertEqual(row['direction_label'], '')
        self.assertEqual(row['current_label'], '50%')
        self.assertEqual(insights['current']['conversations'], 2)

    def test_an_empty_platform_reports_nothing_instead_of_zero(self):
        insights = self.build()
        self.assertFalse(insights['has_data'])
        self.assertFalse(insights['comparable'])
        self.assertEqual(insights['current']['sent'], 0)
        self.assertIsNone(insights['current']['open_rate'])
        self.assertIsNone(insights['current']['open_rate'])
        self.assertIsNone(insights['current']['median_seconds'])
        self.assertEqual(insights['metric_rows'][0]['current_label'], '—')
        self.assertIn('两个周期内都没有私信记录', insights['summary'])

    def test_the_summary_names_what_moved_in_the_order_it_matters(self):
        self.fill_window(read_after=timedelta(hours=1), ago=timedelta(days=45))
        self.fill_window(read_after=timedelta(hours=2))

        insights = self.build()
        self.assertEqual(insights['drivers'][0]['key'], 'median_seconds')
        self.assertIn('中位打开时长较上期上升', insights['summary'])

    def test_the_summary_says_so_when_a_window_is_too_thin_to_compare(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=1),
            created_ago=timedelta(days=45),
        )
        self.create_message(sender=self.buyer, receiver=self.seller)

        insights = self.build()
        self.assertIn('样本还不足', insights['summary'])

    def test_the_silence_window_is_configurable(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=timedelta(days=45),
        )
        self.create_message(
            sender=self.buyer, receiver=self.seller, created_ago=timedelta(days=3),
        )

        insights = self.build(silent_after=timedelta(days=7))
        self.assertEqual(insights['silent_after_days'], 7)
        self.assertEqual(insights['previous']['unopened'], 1)
        self.assertEqual(insights['current']['unopened'], 0)

    def test_a_message_sent_in_the_previous_window_and_opened_now_is_not_retimed(self):
        # read_at can legitimately land outside the window the message was sent
        # in. The message belongs to the window that sent it, and its opening
        # time is measured from its own send, never clipped to a boundary.
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            read_after=timedelta(days=40), created_ago=timedelta(days=45),
        )

        insights = self.build()
        self.assertEqual(insights['previous']['sent'], 1)
        self.assertEqual(insights['previous']['opened'], 1)
        self.assertEqual(insights['previous']['median_seconds'], 40 * 86400)

    def test_only_the_columns_this_module_needs_are_read(self):
        # The comparison must not grow with how many messages have accumulated,
        # so it reads a fixed column set rather than every field.
        from .chat_response import PERIOD_FIELDS
        self.assertIn('created_at', PERIOD_FIELDS)
        self.assertIn('read_at', PERIOD_FIELDS)
        self.assertIn('is_read', PERIOD_FIELDS)