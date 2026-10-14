# -*- coding: utf-8 -*-
"""误判放行的业务闭环测试。

这里不只测状态列变成 ``dismissed``：那条内容必须回到原本要进入的表，接收方必须收到
原路径的通知，历史数据缺材料时也必须显式暴露“没有放行”，不能用文案假装闭环。
"""

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from listings.models import Category, Item, Notification
from .models import Comment, ModerationEvent, PrivateMessage
from .moderation import moderate_submission


class ModerationReleaseTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='release-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='release-buyer', password='safe-password-123')
        self.staff = User.objects.create_user(
            username='release-staff', password='safe-password-123', is_staff=True,
        )
        self.category = Category.objects.create(name='误判放行测试')
        self.item = Item.objects.create(
            title='误判放行测试商品',
            description='测试商品',
            price='10.00',
            category=self.category,
            condition='九成新',
            seller=self.seller,
        )
        self.client.login(username='release-staff', password='safe-password-123')

    def review(self, event, decision='dismissed'):
        return self.client.post(
            reverse('review_moderation_event', args=[event.id]),
            {'decision': decision},
        )

    def test_blocked_private_messages_keep_their_receiver_on_both_write_paths(self):
        self.client.login(username='release-buyer', password='safe-password-123')
        self.client.post(
            reverse('send_message', args=[self.seller.id]),
            {'content': '打开 https://example.com 看看'},
        )
        self.client.post(
            reverse('conversation', args=[self.seller.id]),
            {'content': '再发一次 https://example.com'},
        )

        events = ModerationEvent.objects.filter(
            author=self.buyer, channel='private_message',
        ).order_by('id')
        self.assertEqual(events.count(), 2)
        self.assertEqual({event.receiver for event in events}, {self.seller})
        self.assertEqual(PrivateMessage.objects.count(), 0)

    def test_dismissed_comment_is_restored_and_notifies_the_seller(self):
        event = moderate_submission(
            '打开 https://example.com/course.pdf 看课程资料',
            author=self.buyer, channel='comment', item=self.item,
        )

        self.assertRedirects(self.review(event), reverse('moderation_queue'))

        event.refresh_from_db()
        comment = Comment.objects.get(item=self.item, author=self.buyer)
        self.assertEqual(comment.content, '打开 https://example.com/course.pdf 看课程资料')
        self.assertEqual(event.status, 'dismissed')
        self.assertIsNotNone(event.released_at)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.seller, kind='comment_received',
            ).exists(),
        )
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.buyer, kind='moderation_update',
            ).exists(),
        )

    def test_dismissed_private_message_is_delivered_to_the_saved_receiver(self):
        event = moderate_submission(
            '课程资料在 https://example.com/course.pdf',
            author=self.buyer, channel='private_message', receiver=self.seller,
        )

        self.assertRedirects(self.review(event), reverse('moderation_queue'))

        event.refresh_from_db()
        message = PrivateMessage.objects.get(sender=self.buyer, receiver=self.seller)
        self.assertEqual(message.content, '课程资料在 https://example.com/course.pdf')
        self.assertEqual(event.status, 'dismissed')
        self.assertIsNotNone(event.released_at)
        self.assertTrue(
            Notification.objects.filter(
                recipient=self.seller, kind='message_received',
            ).exists(),
        )

    def test_confirmed_violation_is_not_released(self):
        event = moderate_submission(
            '请加微信 13812345678', author=self.buyer, channel='comment', item=self.item,
        )

        self.assertRedirects(self.review(event, decision='confirmed'), reverse('moderation_queue'))

        event.refresh_from_db()
        self.assertEqual(event.status, 'confirmed')
        self.assertIsNone(event.released_at)
        self.assertEqual(Comment.objects.count(), 0)

    def test_reviewing_a_closed_event_once_does_not_publish_twice(self):
        event = moderate_submission(
            '打开 https://example.com/once', author=self.buyer, channel='comment', item=self.item,
        )
        self.review(event)
        self.review(event)

        self.assertEqual(Comment.objects.count(), 1)
        self.assertEqual(
            Notification.objects.filter(kind='comment_received').count(), 1,
        )

    def test_historical_private_event_without_receiver_is_not_pretended_to_be_released(self):
        event = ModerationEvent.objects.create(
            channel='private_message',
            author=self.buyer,
            content='历史私信缺少接收人',
            matched_terms='外部链接',
            risk_score=20,
            risk_level='low',
        )

        response = self.review(event)

        self.assertRedirects(response, reverse('moderation_queue'))
        event.refresh_from_db()
        self.assertEqual(event.status, 'dismissed')
        self.assertIsNone(event.released_at)
        self.assertEqual(PrivateMessage.objects.count(), 0)
        notice = Notification.objects.get(recipient=self.buyer, kind='moderation_update')
        self.assertIn('未能自动放行', notice.message)

    def test_moderation_capture_keeps_the_full_submittable_content(self):
        content = 'https://example.com/' + ('资料' * 2100)
        event = moderate_submission(
            content, author=self.buyer, channel='comment', item=self.item,
        )

        self.assertEqual(len(event.content), len(content.strip()))
        self.review(event)

        self.assertEqual(Comment.objects.get().content, content)

    def test_overlong_submission_is_rejected_before_an_audit_event_is_created(self):
        self.client.login(username='release-buyer', password='safe-password-123')
        content = 'https://example.com/' + ('长' * 4100)

        response = self.client.post(
            reverse('add_comment', args=[self.item.id]), {'content': content},
        )

        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertFalse(ModerationEvent.objects.exists())
        self.assertFalse(Comment.objects.exists())

    def test_moderation_queue_exposes_release_closure_counts_and_filters(self):
        released = moderate_submission(
            '打开 https://example.com/released',
            author=self.buyer, channel='comment', item=self.item,
        )
        self.review(released)
        released.refresh_from_db()
        missing = ModerationEvent.objects.create(
            channel='private_message',
            author=self.buyer,
            content='历史私信缺少接收人',
            matched_terms='外部链接',
            risk_score=20,
            risk_level='low',
        )

        response = self.client.get(reverse('moderation_queue'), {'release': 'closed'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['moderation_events']), [released])
        self.assertEqual(response.context['moderation_released_count'], 1)
        self.assertEqual(response.context['moderation_unreleased_count'], 0)
        self.assertEqual(response.context['moderation_missing_material_count'], 1)
        self.assertContains(response, '已恢复原渠道')

        response = self.client.get(reverse('moderation_queue'), {'release': 'missing_material'})

        self.assertEqual(list(response.context['moderation_events']), [missing])
        self.assertContains(response, '待复核缺少恢复材料')
