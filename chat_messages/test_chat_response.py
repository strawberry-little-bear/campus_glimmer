from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from listings.models import Category, Item

from .chat_response import (
    MIN_LATENCY_SAMPLE,
    SILENT_AFTER,
    build_chat_response_insights,
    response_seconds_of,
)
from .models import PrivateMessage


class ChatResponseInsightTests(TestCase):
    """Cover the responsiveness metrics derived from the read_at timestamp."""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='chat-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='chat-buyer', password='safe-password-123',
        )
        self.now = timezone.now()
        category = Category.objects.create(name='私信测试分类')
        self.item = Item.objects.create(
            title='私信测试商品', description='用于私信响应时效统计。',
            price='59.00', category=category, condition='九成新',
            seller=self.seller,
        )

    def create_message(self, *, sender, receiver, read_after=None, created_ago=None, item=None):
        """Create a message with a pinned created_at and optional read_at.

        created_at is auto_now_add, so it is rewritten through update() to land
        inside the measured period. The same trick pins read_at, which is the
        only way to build a latency sample without waiting.
        """
        message = PrivateMessage.objects.create(
            sender=sender, receiver=receiver, content='私信响应时效测试。', item=item,
        )
        created_at = self.now - (created_ago or timedelta())
        updates = {'created_at': created_at}
        if read_after is not None:
            updates['is_read'] = True
            updates['read_at'] = created_at + read_after
        PrivateMessage.objects.filter(pk=message.pk).update(**updates)
        message.refresh_from_db()
        return message

    def test_response_seconds_of_returns_none_without_read_at(self):
        message = self.create_message(sender=self.buyer, receiver=self.seller)
        self.assertIsNone(response_seconds_of({
            'read_at': message.read_at,
            'created_at': message.created_at,
        }))

    def test_response_seconds_of_never_goes_negative(self):
        # A clock skew or a backdated write must not produce a negative latency.
        self.assertEqual(
            response_seconds_of({
                'read_at': self.now - timedelta(hours=2),
                'created_at': self.now,
            }),
            0.0,
        )

    def test_latency_uses_only_opened_messages(self):
        opened = self.create_message(
            sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=2),
        )
        self.create_message(sender=self.buyer, receiver=self.seller)

        insights = build_chat_response_insights(days=30, now=self.now)
        summary = insights['summary']
        self.assertEqual(summary['sent'], 2)
        self.assertEqual(summary['opened'], 1)
        self.assertEqual(summary['latency_sample_size'], 1)
        self.assertEqual(summary['median_seconds'], 7200)
        self.assertEqual(summary['median_label'], '2 小时')
        self.assertEqual(opened.is_read, True)

    def test_opened_message_without_read_at_is_not_timed(self):
        # Rows read before read_at existed are opened but carry no timestamp,
        # so they count toward reach and never toward a latency sample.
        self.create_message(sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=1))
        self.create_message(sender=self.buyer, receiver=self.seller)
        PrivateMessage.objects.filter(receiver=self.seller).update(
            is_read=True, read_at=None,
        )

        insights = build_chat_response_insights(days=30, now=self.now)
        summary = insights['summary']
        self.assertEqual(summary['opened'], 2)
        self.assertEqual(summary['latency_sample_size'], 0)
        self.assertEqual(summary['opened_untimed'], 2)
        self.assertIsNone(summary['median_seconds'])

    def test_unopened_messages_are_reported_separately_from_latency(self):
        # The silent one has to be past the silence window, otherwise it is
        # simply still pending and belongs to neither population.
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=SILENT_AFTER + timedelta(hours=1),
        )
        self.create_message(
            sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=5),
        )

        insights = build_chat_response_insights(days=30, now=self.now)
        summary = insights['summary']
        # One silent message, one opened one; the silent one must not be
        # counted as a zero-second reply.
        self.assertEqual(summary['sent'], 2)
        self.assertEqual(summary['unopened'], 1)
        self.assertEqual(summary['latency_sample_size'], 1)
        self.assertEqual(summary['median_seconds'], 18000)

    def test_a_message_inside_the_silence_window_is_not_yet_unopened(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=SILENT_AFTER - timedelta(hours=1),
        )

        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertEqual(insights['summary']['unopened'], 0)

    def test_a_message_past_the_silence_window_is_unopened(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=SILENT_AFTER + timedelta(hours=1),
        )

        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertEqual(insights['summary']['unopened'], 1)

    def test_silent_after_is_configurable(self):
        self.create_message(sender=self.buyer, receiver=self.seller, created_ago=timedelta(days=3))
        insights = build_chat_response_insights(
            days=30, now=self.now, silent_after=timedelta(days=7),
        )
        self.assertEqual(insights['summary']['unopened'], 0)

    def test_role_rows_split_buyer_asks_from_seller_replies(self):
        for index in range(MIN_LATENCY_SAMPLE):
            self.create_message(
                sender=self.buyer, receiver=self.seller, item=self.item,
                read_after=timedelta(hours=index + 1),
            )
            self.create_message(
                sender=self.seller, receiver=self.buyer, item=self.item,
                read_after=timedelta(minutes=index + 1),
            )

        insights = build_chat_response_insights(days=30, now=self.now)
        roles = {row['key']: row for row in insights['role_rows']}
        self.assertEqual(roles['buyer_asks_seller']['sent'], MIN_LATENCY_SAMPLE)
        self.assertEqual(roles['seller_replies_buyer']['sent'], MIN_LATENCY_SAMPLE)
        # The seller takes hours; the buyer replies in minutes. Reporting them
        # as one number would hide exactly the difference worth reading.
        self.assertGreater(
            roles['buyer_asks_seller']['median_seconds'],
            roles['seller_replies_buyer']['median_seconds'],
        )
        self.assertTrue(roles['buyer_asks_seller']['has_sample'])
        self.assertTrue(roles['seller_replies_buyer']['has_sample'])

    def test_role_rows_do_not_force_itemless_messages_into_a_role(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller, read_after=timedelta(hours=1),
        )
        insights = build_chat_response_insights(days=30, now=self.now)
        roles = {row['key']: row for row in insights['role_rows']}
        self.assertEqual(roles['no_item']['sent'], 1)
        self.assertEqual(roles['buyer_asks_seller']['sent'], 0)

    def test_role_sample_below_threshold_is_flagged_as_insufficient(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=self.item,
            read_after=timedelta(hours=1),
        )
        insights = build_chat_response_insights(days=30, now=self.now)
        roles = {row['key']: row for row in insights['role_rows']}
        self.assertFalse(roles['buyer_asks_seller']['has_sample'])

    def test_latency_distribution_buckets_every_opened_message(self):
        for minutes, expected in ((30, '1 小时内'), (120, '1 天内'), (7200, '1 周内')):
            self.create_message(
                sender=self.buyer, receiver=self.seller,
                read_after=timedelta(minutes=minutes),
            )
        insights = build_chat_response_insights(days=30, now=self.now)
        buckets = {row['label']: row['count'] for row in insights['latency_buckets']}
        self.assertEqual(buckets['1 小时内'], 1)
        self.assertEqual(buckets['1 天内'], 1)
        self.assertEqual(buckets['1 周内'], 1)
        self.assertEqual(insights['latency_total'], 3)

    def test_conversation_health_counts_two_way_and_single_message_threads(self):
        third = User.objects.create_user(username='chat-third', password='safe-password-123')
        # Two-way: both sides sent.
        self.create_message(sender=self.buyer, receiver=self.seller)
        self.create_message(sender=self.seller, receiver=self.buyer)
        # One-way: only the buyer ever sent.
        self.create_message(sender=self.buyer, receiver=third)

        insights = build_chat_response_insights(days=30, now=self.now)
        health = insights['conversation_health']
        self.assertEqual(health['total'], 2)
        self.assertEqual(health['two_way'], 1)
        self.assertEqual(health['two_way_rate'], 50.0)
        self.assertEqual(health['single_message'], 1)

    def test_conversation_health_reports_never_opened_conversations(self):
        self.create_message(sender=self.buyer, receiver=self.seller)
        self.create_message(sender=self.buyer, receiver=self.seller)
        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertEqual(insights['conversation_health']['never_opened'], 1)

    def test_daily_trend_covers_every_day_in_the_period(self):
        insights = build_chat_response_insights(days=7, now=self.now)
        trend = insights['daily_trend']
        self.assertEqual(len(trend), 7)
        self.assertEqual(
            [point['sent'] for point in trend],
            [0] * 7,
        )

    def test_daily_trend_counts_a_message_on_the_day_it_was_opened(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=timedelta(days=2), read_after=timedelta(days=1),
        )
        insights = build_chat_response_insights(days=7, now=self.now)
        trend = {point['date']: point for point in insights['daily_trend']}
        sent_day = timezone.localdate(self.now - timedelta(days=2))
        opened_day = timezone.localdate(self.now - timedelta(days=1))
        self.assertEqual(trend[sent_day]['sent'], 1)
        self.assertEqual(trend[opened_day]['opened'], 1)

    def test_empty_period_reports_no_data_instead_of_zeroes(self):
        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertFalse(insights['has_data'])
        self.assertEqual(insights['summary']['sent'], 0)
        self.assertIsNone(insights['summary']['median_seconds'])
        self.assertEqual(insights['summary']['open_rate'], 0)

    def test_hour_profile_covers_all_twenty_four_hours(self):
        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertEqual(len(insights['hour_profile']), 24)
        self.assertEqual(insights['hour_profile'][0]['label'], '00:00')

    def test_messages_outside_the_period_are_excluded(self):
        self.create_message(
            sender=self.buyer, receiver=self.seller,
            created_ago=timedelta(days=40), read_after=timedelta(hours=1),
        )
        insights = build_chat_response_insights(days=30, now=self.now)
        self.assertFalse(insights['has_data'])
        self.assertEqual(insights['summary']['sent'], 0)







class MessageReadTimestampTests(TestCase):
    """Every path that flips is_read must also record when it happened."""

    def setUp(self):
        self.seller = User.objects.create_user(
            username='stamp-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='stamp-buyer', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='stamp-other', password='safe-password-123',
        )

    def make_message(self, *, sender, receiver, content):
        return PrivateMessage.objects.create(
            sender=sender, receiver=receiver, content=content,
        )

    def test_opening_a_conversation_records_the_first_read_moment(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        response = self.client.get(reverse('conversation', args=[self.buyer.id]))
        self.assertEqual(response.status_code, 200)
        message.refresh_from_db()
        self.assertTrue(message.is_read)
        self.assertIsNotNone(message.read_at)

    def test_marking_one_message_read_records_the_first_read_moment(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        response = self.client.post(reverse('mark_message_read', args=[message.id]))
        self.assertEqual(response.status_code, 302)
        message.refresh_from_db()
        self.assertTrue(message.is_read)
        self.assertIsNotNone(message.read_at)

    def test_marking_every_message_read_records_the_first_read_moment(self):
        first = self.make_message(
            sender=self.buyer, receiver=self.seller, content='第一条',
        )
        second = self.make_message(
            sender=self.buyer, receiver=self.seller, content='第二条',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        response = self.client.post(reverse('mark_all_messages_read'))
        self.assertEqual(response.status_code, 302)
        for message in (first, second):
            message.refresh_from_db()
            self.assertTrue(message.is_read)
            self.assertIsNotNone(message.read_at)

    def test_marking_selected_messages_read_records_the_first_read_moment(self):
        selected = self.make_message(
            sender=self.buyer, receiver=self.seller, content='选中',
        )
        self.make_message(
            sender=self.buyer, receiver=self.seller, content='未选中',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        response = self.client.post(
            reverse('mark_selected_messages_read'),
            {'message_ids': [str(selected.id)]},
        )
        self.assertEqual(response.status_code, 302)
        selected.refresh_from_db()
        self.assertTrue(selected.is_read)
        self.assertIsNotNone(selected.read_at)

    def test_marking_a_whole_conversation_read_records_the_first_read_moment(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        response = self.client.post(reverse('mark_conversation_read', args=[self.buyer.id]))
        self.assertEqual(response.status_code, 302)
        message.refresh_from_db()
        self.assertTrue(message.is_read)
        self.assertIsNotNone(message.read_at)

    def test_batch_paths_share_one_timestamp_for_one_user_action(self):
        # Opening a conversation marks the whole thread at once. Those rows
        # must carry the same moment, otherwise one click is split into
        # several slightly different instants and the latency is not honest.
        first = self.make_message(
            sender=self.buyer, receiver=self.seller, content='第一条',
        )
        second = self.make_message(
            sender=self.buyer, receiver=self.seller, content='第二条',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        self.client.get(reverse('conversation', args=[self.buyer.id]))
        first.refresh_from_db()
        second.refresh_from_db()
        self.assertEqual(first.read_at, second.read_at)

    def test_a_second_read_does_not_overwrite_the_first_moment(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')
        self.client.get(reverse('conversation', args=[self.buyer.id]))
        message.refresh_from_db()
        first_stamp = message.read_at

        # Re-opening the conversation and re-running the batch path must leave
        # the original timestamp alone.
        self.client.get(reverse('conversation', args=[self.buyer.id]))
        message.refresh_from_db()
        self.assertEqual(message.read_at, first_stamp)

    def test_read_at_is_written_after_created_at(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')
        self.client.post(reverse('mark_message_read', args=[message.id]))
        message.refresh_from_db()
        self.assertIsNotNone(message.read_at)
        self.assertGreaterEqual(message.read_at, message.created_at)

    def test_a_message_left_unread_keeps_a_null_read_at(self):
        message = self.make_message(
            sender=self.buyer, receiver=self.seller, content='一条未读私信',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')
        self.client.get(reverse('conversation', args=[self.other.id]))
        message.refresh_from_db()
        self.assertFalse(message.is_read)
        self.assertIsNone(message.read_at)

    def test_marking_a_conversation_read_leaves_other_senders_untouched(self):
        from_buyer = self.make_message(
            sender=self.buyer, receiver=self.seller, content='买家',
        )
        from_other = self.make_message(
            sender=self.other, receiver=self.seller, content='其他人',
        )
        self.client.login(username='stamp-seller', password='safe-password-123')

        self.client.post(reverse('mark_conversation_read', args=[self.buyer.id]))
        from_buyer.refresh_from_db()
        from_other.refresh_from_db()
        self.assertIsNotNone(from_buyer.read_at)
        self.assertIsNone(from_other.read_at)
        self.assertFalse(from_other.is_read)

