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
