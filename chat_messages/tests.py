from django.contrib.auth.models import User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from listings.models import CampusLocation, Category, Item, Notification
from .models import Comment, ModerationEvent, PrivateMessage
from .moderation import analyze_content
from .views import _build_conversation_data


class MessageNotificationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='buyer', password='safe-password-123')
        self.staff = User.objects.create_user(username='moderator', password='safe-password-123', is_staff=True)
        category = Category.objects.create(name='生活用品')
        self.item = Item.objects.create(
            title='宿舍台灯', description='暖光台灯', price='39.00', category=category,
            condition='全新', seller=self.seller,
        )

    def test_comment_notifies_item_seller(self):
        self.client.login(username='buyer', password='safe-password-123')
        response = self.client.post(
            reverse('add_comment', args=[self.item.id]),
            {'content': '请问还在吗？'},
        )
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertTrue(Comment.objects.filter(item=self.item, author=self.buyer).exists())
        notice = Notification.objects.get(recipient=self.seller, kind='comment_received')
        self.assertEqual(notice.actor, self.buyer)
        self.assertEqual(notice.item, self.item)

    def test_private_message_notifies_receiver(self):
        self.client.login(username='buyer', password='safe-password-123')
        response = self.client.post(
            reverse('send_message', args=[self.seller.id]),
            {'content': '你好，这件商品还在吗？'},
        )
        self.assertRedirects(response, reverse('conversation', args=[self.seller.id]))
        self.assertTrue(PrivateMessage.objects.filter(sender=self.buyer, receiver=self.seller).exists())
        notice = Notification.objects.get(recipient=self.seller, kind='message_received')
        self.assertEqual(notice.actor, self.buyer)

    def test_inbox_searches_messages_and_bulk_marks_unread_as_read(self):
        PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller,
            content='想了解台灯的电池续航', item=self.item,
        )
        PrivateMessage.objects.create(
            sender=self.seller, receiver=self.buyer,
            content='可以当面演示，周末方便吗？', item=self.item,
        )
        PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller,
            content='这条消息用于其他关键词测试',
        )

        self.client.login(username='seller', password='safe-password-123')
        response = self.client.get(reverse('inbox'), {'q': '续航'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '想了解台灯的电池续航')
        self.assertEqual(response.context['received_messages'].count(), 1)
        self.assertEqual(response.context['sent_messages'].count(), 0)
        self.assertEqual(response.context['conversation_data'][0]['message_count'], 3)

        response = self.client.post(reverse('mark_all_messages_read'))
        self.assertRedirects(response, reverse('inbox'))
        self.assertEqual(PrivateMessage.objects.filter(receiver=self.seller, is_read=False).count(), 0)

    def test_inbox_can_filter_received_messages_by_read_state(self):
        unread = PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller, content='这是一条未读消息',
        )
        read = PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller, content='这是一条已读消息', is_read=True,
        )
        self.client.login(username='seller', password='safe-password-123')

        unread_response = self.client.get(reverse('inbox'), {'status': 'unread'})
        self.assertEqual(unread_response.status_code, 200)
        self.assertEqual(list(unread_response.context['received_messages']), [unread])
        self.assertEqual(unread_response.context['message_status_filter'], 'unread')
        self.assertContains(unread_response, '已应用筛选条件')

        read_response = self.client.get(reverse('inbox'), {'status': 'read'})
        self.assertEqual(list(read_response.context['received_messages']), [read])
        self.assertEqual(read_response.context['message_filtered_unread_count'], 0)

    def test_conversation_summary_uses_bounded_queries(self):
        for index in range(4):
            PrivateMessage.objects.create(
                sender=self.buyer, receiver=self.seller,
                content=f'消息 {index}', item=self.item,
            )
            PrivateMessage.objects.create(
                sender=self.seller, receiver=self.buyer,
                content=f'回复 {index}', item=self.item,
            )

        conversation_users = User.objects.filter(
            sent_messages__receiver=self.seller,
        ).distinct()
        with CaptureQueriesContext(connection) as queries:
            data = _build_conversation_data(self.seller, conversation_users)

        self.assertLessEqual(len(queries), 2)
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]['message_count'], 8)
        self.assertEqual(data[0]['unread_count'], 4)

    def test_inbox_uses_latest_message_in_both_directions(self):
        PrivateMessage.objects.create(
            sender=self.buyer, receiver=self.seller, content='先发的消息',
        )
        PrivateMessage.objects.create(
            sender=self.seller, receiver=self.buyer, content='后发的回复',
        )

        self.client.login(username='seller', password='safe-password-123')
        response = self.client.get(reverse('inbox'))
        self.assertEqual(response.status_code, 200)
        conversation = response.context['conversation_data'][0]
        self.assertEqual(conversation['last_message'].content, '后发的回复')

class UnreadSummaryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='reader', password='safe-password-123')
        self.other_user = User.objects.create_user(username='other-reader', password='safe-password-123')

    def test_unread_summary_returns_only_current_user_counts(self):
        Notification.objects.create(
            recipient=self.user, kind='order_status', title='订单更新', message='你的订单有新的状态。',
        )
        Notification.objects.create(
            recipient=self.user, kind='comment_received', title='商品留言', message='你的商品收到新的留言。', is_read=True,
        )
        Notification.objects.create(
            recipient=self.other_user, kind='order_status', title='订单更新', message='另一位用户的订单有新的状态。',
        )
        PrivateMessage.objects.create(
            sender=self.other_user, receiver=self.user, content='有一条未读私信。',
        )
        PrivateMessage.objects.create(
            sender=self.user, receiver=self.other_user, content='这是发给别人的消息。',
        )

        self.client.login(username='reader', password='safe-password-123')
        response = self.client.get(reverse('unread_summary'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {'notifications': 1, 'messages': 1})

    def test_unread_summary_requires_login(self):
        response = self.client.get(reverse('unread_summary'))

        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)


class ModerationFlowTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='mod-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='mod-buyer', password='safe-password-123')
        self.staff = User.objects.create_user(
            username='mod-staff', password='safe-password-123', is_staff=True,
        )
        category = Category.objects.create(name='审核测试')
        self.item = Item.objects.create(
            title='审核测试商品', description='测试商品', price='10.00', category=category,
            condition='九成新', seller=self.seller,
        )

    def test_risky_comment_is_blocked_and_recorded(self):
        self.client.login(username='mod-buyer', password='safe-password-123')
        response = self.client.post(
            reverse('add_comment', args=[self.item.id]),
            {'content': '请加微信 13812345678 私下转账'},
        )
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertFalse(Comment.objects.filter(item=self.item, author=self.buyer).exists())
        event = ModerationEvent.objects.get(author=self.buyer, channel='comment')
        self.assertEqual(event.status, 'pending')
        self.assertIn('微信导流', event.matched_terms)
        self.assertIn('手机号', event.matched_terms)
        self.assertIn('高风险交易', event.matched_terms)
        self.assertEqual(event.risk_score, 100)
        self.assertEqual(event.risk_level, 'high')

    def test_risky_private_message_is_blocked_before_delivery(self):
        self.client.login(username='mod-buyer', password='safe-password-123')
        response = self.client.post(
            reverse('send_message', args=[self.seller.id]),
            {'content': '打开 https://example.com 看看'},
        )
        self.assertEqual(response.status_code, 200)
        self.assertFalse(PrivateMessage.objects.filter(sender=self.buyer, receiver=self.seller).exists())
        self.assertTrue(ModerationEvent.objects.filter(author=self.buyer, channel='private_message').exists())
        self.assertContains(response, '暂未发送')

    def test_staff_review_updates_event_and_notifies_author(self):
        event = ModerationEvent.objects.create(
            channel='comment', author=self.buyer, item=self.item,
            content='请加微信', matched_terms='微信导流',
        )
        self.client.login(username='mod-staff', password='safe-password-123')
        response = self.client.post(
            reverse('review_moderation_event', args=[event.id]),
            {'decision': 'confirmed'},
        )
        self.assertRedirects(response, reverse('moderation_queue'))
        event.refresh_from_db()
        self.assertEqual(event.status, 'confirmed')
        self.assertEqual(event.reviewed_by, self.staff)
        notice = Notification.objects.get(recipient=self.buyer, kind='moderation_update')
        self.assertIn('确认违规', notice.title)

    def test_risk_analysis_is_explainable_and_capped(self):
        analysis = analyze_content('请发微信号，手机号 13812345678，押金和 https://example.com')
        self.assertEqual(analysis['matched_terms'], ['微信导流', '外部链接', '手机号', '高风险交易'])
        self.assertEqual(analysis['risk_score'], 100)
        self.assertEqual(analysis['risk_level'], 'high')

    def test_moderation_queue_can_filter_high_risk_events(self):
        ModerationEvent.objects.create(
            channel='comment', author=self.buyer, item=self.item,
            content='外链', matched_terms='外部链接', risk_score=20, risk_level='low',
        )
        high = ModerationEvent.objects.create(
            channel='private_message', author=self.buyer,
            content='押金和手机号', matched_terms='高风险交易、手机号',
            risk_score=85, risk_level='high',
        )
        self.client.login(username='mod-staff', password='safe-password-123')
        response = self.client.get(reverse('moderation_queue'), {'risk': 'high'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['moderation_events']), [high])
        self.assertEqual(response.context['moderation_pending_high_count'], 1)

    def test_non_staff_cannot_open_moderation_queue(self):
        self.client.login(username='mod-buyer', password='safe-password-123')
        response = self.client.get(reverse('moderation_queue'))
        self.assertEqual(response.status_code, 403)
