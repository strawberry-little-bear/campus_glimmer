from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from listings.models import Category, Item, Order, OrderEvent

from listings.operations_panel_sections import export_section_text, panel_sections
from .chat_trade_correlation import MIN_BUCKET_SAMPLE, build_chat_trade_correlation
from .models import PrivateMessage


class ChatTradeCorrelationTests(TestCase):
    """Keep the co-variation denominator honest and free of identities."""

    def setUp(self):
        self.now = timezone.now()
        self.seller = User.objects.create_user(
            username='trade-seller', password='safe-password-123',
        )
        self.buyer = User.objects.create_user(
            username='trade-buyer', password='safe-password-123',
        )
        self.category = Category.objects.create(name='响应成交测试分类')

    def make_item(self, title):
        return Item.objects.create(
            title=title,
            description='用于私信响应与成交共同变化统计。',
            price='38.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
        )

    def create_message(
        self, *, sender, receiver, item, created_at, read_at=None, content='响应成交测试私信',
    ):
        message = PrivateMessage.objects.create(
            sender=sender,
            receiver=receiver,
            item=item,
            content=content,
        )
        updates = {'created_at': created_at}
        if read_at is not None:
            updates['is_read'] = True
            updates['read_at'] = read_at
        PrivateMessage.objects.filter(pk=message.pk).update(**updates)
        message.refresh_from_db()
        return message

    def create_order(self, *, item, buyer, created_at, terminal=None, terminal_at=None):
        order = Order.objects.create(
            item=item,
            buyer=buyer,
            seller=self.seller,
            agreed_price=item.price,
            status=terminal or 'pending',
        )
        Order.objects.filter(pk=order.pk).update(created_at=created_at)
        if terminal:
            event = OrderEvent.objects.create(
                order=order,
                actor=self.seller,
                from_status='pending',
                to_status=terminal,
                note='响应成交测试事件',
            )
            OrderEvent.objects.filter(pk=event.pk).update(
                created_at=terminal_at or created_at,
            )
        order.refresh_from_db()
        return order

    def eligible_inquiry(self, *, item=None, buyer=None, latency=timedelta(minutes=30)):
        item = item or self.make_item('响应成交商品')
        buyer = buyer or self.buyer
        first_at = self.now - timedelta(days=8)
        return item, self.create_message(
            sender=buyer,
            receiver=self.seller,
            item=item,
            created_at=first_at,
            read_at=first_at + latency,
        )

    def test_empty_period_keeps_all_fixed_rows(self):
        result = build_chat_trade_correlation(days=30, now=self.now)

        self.assertFalse(result['has_data'])
        self.assertEqual(
            [row['label'] for row in result['rows']],
            ['1 小时内', '1 天内', '3 天内', '1 周内', '1 周以上'],
        )
        self.assertTrue(all(row['conversations'] == 0 for row in result['rows']))
        self.assertEqual(result['summary']['observed_conversations'], 0)
        self.assertIsNone(result['summary']['completion_rate'])

    def test_bucket_order_and_zero_bucket_are_preserved(self):
        for latency in (
            timedelta(minutes=30), timedelta(hours=2), timedelta(days=2),
            timedelta(days=5),
        ):
            self.eligible_inquiry(latency=latency)

        result = build_chat_trade_correlation(days=30, now=self.now)
        counts = {row['label']: row['conversations'] for row in result['rows']}

        self.assertEqual(
            counts,
            {'1 小时内': 1, '1 天内': 1, '3 天内': 1, '1 周内': 1, '1 周以上': 0},
        )
        self.assertEqual(result['summary']['eligible_conversations'], 4)

    def test_old_conversation_touched_in_period_is_not_new(self):
        item = self.make_item('旧会话商品')
        first_at = self.now - timedelta(days=40)
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=item, created_at=first_at,
        )
        recent_at = self.now - timedelta(days=8)
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=item, created_at=recent_at,
            read_at=recent_at + timedelta(minutes=20),
        )

        result = build_chat_trade_correlation(days=30, now=self.now)

        self.assertEqual(result['summary']['observed_conversations'], 1)
        self.assertEqual(result['summary']['started_before_period'], 1)
        self.assertEqual(result['summary']['eligible_conversations'], 0)

    def test_immature_and_untimed_conversations_stay_out_of_denominator(self):
        immature_item = self.make_item('未满窗口商品')
        immature_at = self.now - timedelta(days=3)
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=immature_item,
            created_at=immature_at, read_at=immature_at + timedelta(minutes=10),
        )
        untimed_item = self.make_item('无法计时商品')
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=untimed_item,
            created_at=self.now - timedelta(days=8),
        )

        result = build_chat_trade_correlation(days=30, now=self.now)

        self.assertEqual(result['summary']['immature_conversations'], 1)
        self.assertEqual(result['summary']['untimed_conversations'], 1)
        self.assertEqual(result['summary']['eligible_conversations'], 0)
        self.assertTrue(all(row['conversations'] == 0 for row in result['rows']))

    def test_one_conversation_counts_once_despite_multiple_inquiries(self):
        item = self.make_item('多条询问商品')
        first_at = self.now - timedelta(days=8)
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=item, created_at=first_at,
            read_at=first_at + timedelta(hours=1),
        )
        second_at = self.now - timedelta(days=7)
        self.create_message(
            sender=self.buyer, receiver=self.seller, item=item, created_at=second_at,
            read_at=second_at + timedelta(minutes=30),
        )

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['conversations'], 1)
        self.assertEqual(row['timed_inquiries'], 2)
        self.assertEqual(row['median_label'], '30 分钟')

    def test_buyers_on_the_same_item_are_separate_conversations(self):
        other_buyer = User.objects.create_user(username='trade-other-buyer')
        item = self.make_item('多买家商品')
        self.eligible_inquiry(item=item, buyer=self.buyer)
        self.eligible_inquiry(item=item, buyer=other_buyer)
        self.create_order(
            item=item, buyer=self.buyer,
            created_at=self.now - timedelta(days=7), terminal='completed',
        )

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['conversations'], 2)
        self.assertEqual(row['ordered'], 1)
        self.assertEqual(row['completed'], 1)
        self.assertEqual(row['completion_rate'], 50.0)

    def test_order_created_before_first_inquiry_is_not_an_outcome(self):
        item, _message = self.eligible_inquiry()
        self.create_order(
            item=item, buyer=self.buyer,
            created_at=self.now - timedelta(days=9), terminal='completed',
            terminal_at=self.now - timedelta(days=7),
        )

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['ordered'], 0)
        self.assertEqual(row['completed'], 0)

    def test_order_or_terminal_event_after_window_is_not_an_outcome(self):
        item, _message = self.eligible_inquiry()
        order = self.create_order(item=item, buyer=self.buyer, created_at=self.now)
        event = OrderEvent.objects.create(
            order=order, actor=self.seller, from_status='pending',
            to_status='completed', note='窗口外事件',
        )
        OrderEvent.objects.filter(pk=event.pk).update(created_at=self.now)

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['ordered'], 0)
        self.assertEqual(row['completed'], 0)

    def test_completed_and_returned_both_count_as_closed_trades(self):
        for terminal in ('completed', 'returned'):
            item, _message = self.eligible_inquiry()
            self.create_order(
                item=item, buyer=self.buyer,
                created_at=self.now - timedelta(days=7), terminal=terminal,
            )

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['ordered'], 2)
        self.assertEqual(row['completed'], 2)
        self.assertEqual(row['completion_rate'], 100.0)

    def test_bucket_needs_ten_mature_conversations_for_a_usable_rate(self):
        for index in range(MIN_BUCKET_SAMPLE):
            self.eligible_inquiry()

        result = build_chat_trade_correlation(days=30, now=self.now)
        row = result['rows'][0]

        self.assertEqual(row['conversations'], MIN_BUCKET_SAMPLE)
        self.assertTrue(row['has_sample'])
        self.assertEqual(row['completion_rate'], 0.0)

    def test_returned_payload_has_no_private_identity_or_content(self):
        item, _message = self.eligible_inquiry()
        order = self.create_order(
            item=item, buyer=self.buyer,
            created_at=self.now - timedelta(days=7), terminal='completed',
        )

        result = build_chat_trade_correlation(days=30, now=self.now)
        rendered = repr(result)

        self.assertNotIn(self.buyer.username, rendered)
        self.assertNotIn(self.seller.username, rendered)
        self.assertNotIn(item.title, rendered)
        self.assertNotIn('响应成交测试私信', rendered)
        self.assertEqual(
            set(result['rows'][0]),
            {
                'key', 'label', 'conversations', 'timed_inquiries',
                'median_seconds', 'median_label', 'ordered', 'completed',
                'completion_rate', 'has_sample',
            },
        )

    def test_dashboard_and_export_are_structured_and_private(self):
        staff = User.objects.create_user(
            username='trade-staff', password='safe-password-123', is_staff=True,
        )
        item, _message = self.eligible_inquiry()
        order = self.create_order(
            item=item, buyer=self.buyer,
            created_at=self.now - timedelta(days=7), terminal='completed',
        )
        self.client.force_login(staff)

        dashboard = self.client.get(reverse('operations_dashboard'), {'days': 30})
        export = self.client.get(
            reverse('operations_dashboard_export'), {'days': 30},
        )

        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(export.status_code, 200)
        html_section = dashboard.content.decode('utf-8').split(
            'chat-trade-correlation-panel', 1,
        )[1].split('recommendation-quality-panel', 1)[0]
        csv_section = export_section_text(
            export.content.decode('utf-8-sig'), '私信响应与成交的共同变化',
            panel_sections(dashboard.content.decode('utf-8')),
        )

        for text in (html_section, csv_section):
            self.assertIn('响应档', text)
            self.assertIn('会话', text)
            self.assertIn('带时戳询问', text)
            self.assertIn('中位打开', text)
            self.assertIn('有订单', text)
            self.assertIn('7天内成交', text)
            self.assertIn('成交率', text)
            self.assertIn('样本', text)
            self.assertNotIn(self.buyer.username, text)
            self.assertNotIn(self.seller.username, text)
            self.assertNotIn(item.title, text)
            self.assertNotIn('响应成交测试私信', text)
