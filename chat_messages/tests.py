from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from listings.models import CampusLocation, Category, Item, Notification
from .models import Comment, PrivateMessage


class MessageNotificationTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='buyer', password='safe-password-123')
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
