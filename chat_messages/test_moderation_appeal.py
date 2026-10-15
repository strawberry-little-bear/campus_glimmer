# -*- coding: utf-8 -*-
"""内容申诉闭环测试。

申诉的价值不在多一张表，而在三个事实同时成立：用户只能申诉一次，运营改判必须
真正恢复内容，原复核事实不能被改判覆盖。这里按这三条链路做验证。
"""

from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from listings.models import Category, Item, Notification
from .admin import ModerationAppealAdmin
from .models import Comment, ModerationAppeal, ModerationEvent, PrivateMessage
from .moderation import moderate_submission


class ModerationAppealBaseTestCase(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(username='appeal-seller', password='safe-password-123')
        self.buyer = User.objects.create_user(username='appeal-buyer', password='safe-password-123')
        self.other = User.objects.create_user(username='appeal-other', password='safe-password-123')
        self.first_reviewer = User.objects.create_user(
            username='appeal-first-reviewer', password='safe-password-123', is_staff=True,
        )
        self.final_reviewer = User.objects.create_user(
            username='appeal-final-reviewer', password='safe-password-123', is_staff=True,
        )
        self.category = Category.objects.create(name='内容申诉测试')
        self.item = Item.objects.create(
            title='内容申诉测试商品', description='测试商品', price='10.00',
            category=self.category, condition='九成新', seller=self.seller,
        )
        self.reviewed_at = timezone.now() - timedelta(days=2)

    def confirmed_event(
        self, *, content='请加微信沟通', author=None, reviewer=None,
        channel='comment', receiver=None,
    ):
        event = moderate_submission(
            content, author=author or self.buyer,
            channel=channel, item=self.item, receiver=receiver,
        )
        event.status = 'confirmed'
        event.reviewed_by = reviewer or self.first_reviewer
        event.reviewed_at = self.reviewed_at
        event.save(update_fields=['status', 'reviewed_by', 'reviewed_at'])
        return event

    def submit_appeal(self, event, *, reason='missing_context', statement='这是课程交易语境。'):
        return ModerationAppeal.objects.create(
            event=event, appellant=event.author, reason=reason, statement=statement,
            original_reviewed_by=event.reviewed_by,
            original_reviewed_at=event.reviewed_at,
        )

    def resolve(self, appeal, *, decision='accepted', note='复核后确认是课程语境。', next_url=''):
        data = {'decision': decision, 'resolution_note': note}
        if next_url:
            data['next'] = next_url
        return self.client.post(reverse('resolve_moderation_appeal', args=[appeal.id]), data)


class ModerationAppealSubmissionTests(ModerationAppealBaseTestCase):
    def test_user_page_only_lists_own_confirmed_events(self):
        confirmed = self.confirmed_event()
        pending = moderate_submission(
            '打开 https://example.com/pending', author=self.buyer,
            channel='comment', item=self.item,
        )
        other_event = self.confirmed_event(
            content='其他用户加微信沟通', author=self.other,
        )
        self.client.login(username='appeal-buyer', password='safe-password-123')

        response = self.client.get(reverse('moderation_appeals'))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(response.context['events']), [confirmed])
        self.assertNotContains(response, other_event.content)
        self.assertNotContains(response, pending.content)
        self.assertContains(response, '提交申诉')

    def test_confirmed_author_can_submit_one_appeal_with_original_review_snapshot(self):
        event = self.confirmed_event()
        self.client.login(username='appeal-buyer', password='safe-password-123')

        response = self.client.post(reverse('submit_moderation_appeal', args=[event.id]), {
            'reason': 'missing_context',
            'statement': '这是课程交易语境。',
        })
        appeal = event.appeal

        self.assertRedirects(response, reverse('moderation_appeals'))
        self.assertEqual(appeal.appellant, self.buyer)
        self.assertEqual(appeal.status, 'pending')
        self.assertEqual(appeal.original_reviewed_by, self.first_reviewer)
        self.assertEqual(appeal.original_reviewed_at, self.reviewed_at)

        response = self.client.post(reverse('submit_moderation_appeal', args=[event.id]), {
            'reason': 'other', 'statement': '想再提交一次。',
        })
        self.assertRedirects(response, reverse('moderation_appeals'))
        self.assertEqual(ModerationAppeal.objects.count(), 1)

    def test_pending_event_other_author_and_invalid_statement_are_rejected(self):
        pending = moderate_submission(
            '打开 https://example.com/pending', author=self.buyer,
            channel='comment', item=self.item,
        )
        other_event = self.confirmed_event(author=self.other)
        confirmed = self.confirmed_event()
        self.client.login(username='appeal-buyer', password='safe-password-123')

        response = self.client.post(reverse('submit_moderation_appeal', args=[pending.id]), {
            'reason': 'other', 'statement': '暂未确认违规。',
        })
        self.assertRedirects(response, reverse('moderation_appeals'))
        self.assertFalse(ModerationAppeal.objects.exists())

        response = self.client.post(reverse('submit_moderation_appeal', args=[other_event.id]), {
            'reason': 'other', 'statement': '这不是我的内容。',
        })
        self.assertEqual(response.status_code, 404)

        response = self.client.post(reverse('submit_moderation_appeal', args=[confirmed.id]), {
            'reason': 'other', 'statement': '',
        })
        self.assertRedirects(response, reverse('moderation_appeals'))
        self.assertFalse(ModerationAppeal.objects.exists())


class ModerationAppealQueueTests(ModerationAppealBaseTestCase):
    def test_pending_appeals_are_filterable_and_sorted_first(self):
        appealed_event = self.confirmed_event(content='押金说明')
        appeal = self.submit_appeal(appealed_event)
        ordinary_pending = moderate_submission(
            '打开 https://example.com/ordinary', author=self.buyer,
            channel='comment', item=self.item,
        )
        self.client.login(username='appeal-final-reviewer', password='safe-password-123')

        response = self.client.get(reverse('moderation_queue'), {'appeal': 'pending'})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.context['moderation_status_filter'], 'all')
        self.assertEqual(response.context['moderation_appeal_filter'], 'pending')
        self.assertEqual(list(response.context['moderation_events']), [appealed_event])
        self.assertEqual(response.context['moderation_appeal_pending_count'], 1)
        self.assertContains(response, '用户申诉')

        response = self.client.get(reverse('moderation_queue'), {'status': 'all'})
        self.assertEqual(list(response.context['moderation_events']), [
            appealed_event, ordinary_pending,
        ])


class ModerationAppealResolutionTests(ModerationAppealBaseTestCase):
    def setUp(self):
        super().setUp()
        self.client.login(username='appeal-final-reviewer', password='safe-password-123')

    def test_accepted_appeal_changes_event_restores_comment_and_keeps_snapshot(self):
        event = self.confirmed_event()
        appeal = self.submit_appeal(event)

        response = self.resolve(appeal, next_url=reverse('moderation_appeals'))

        self.assertRedirects(response, reverse('moderation_appeals'))
        event.refresh_from_db()
        appeal.refresh_from_db()
        comment = Comment.objects.get(item=self.item, author=self.buyer)
        self.assertEqual(comment.content, event.content)
        self.assertEqual(event.status, 'dismissed')
        self.assertEqual(event.reviewed_by, self.final_reviewer)
        self.assertIsNotNone(event.released_at)
        self.assertEqual(appeal.status, 'accepted')
        self.assertEqual(appeal.resolved_by, self.final_reviewer)
        self.assertEqual(appeal.original_reviewed_by, self.first_reviewer)
        self.assertEqual(appeal.original_reviewed_at, self.reviewed_at)
        self.assertTrue(Notification.objects.filter(
            recipient=self.seller, kind='comment_received',
        ).exists())
        self.assertTrue(Notification.objects.filter(
            recipient=self.buyer, kind='moderation_update',
        ).exists())

    def test_missing_material_acceptance_does_not_pretend_to_restore_content(self):
        event = self.confirmed_event(
            content='请加微信接收私信', channel='private_message', receiver=self.seller,
        )
        ModerationEvent.objects.filter(id=event.id).update(receiver=None)
        event.refresh_from_db()
        appeal = self.submit_appeal(event)

        response = self.resolve(appeal)

        self.assertRedirects(response, reverse('moderation_queue'))
        event.refresh_from_db()
        appeal.refresh_from_db()
        self.assertEqual(event.status, 'dismissed')
        self.assertIsNone(event.released_at)
        self.assertEqual(appeal.status, 'accepted_without_restore')
        self.assertEqual(PrivateMessage.objects.count(), 0)
        notice = Notification.objects.get(recipient=self.buyer, kind='moderation_update')
        self.assertIn('未能自动放行', notice.message)

    def test_upheld_appeal_keeps_confirmed_event_without_publishing(self):
        event = self.confirmed_event()
        appeal = self.submit_appeal(event)

        response = self.resolve(appeal, decision='upheld', note='仍包含脱离平台联系风险。')

        self.assertRedirects(response, reverse('moderation_queue'))
        event.refresh_from_db()
        appeal.refresh_from_db()
        self.assertEqual(event.status, 'confirmed')
        self.assertEqual(event.reviewed_by, self.first_reviewer)
        self.assertEqual(event.reviewed_at, self.reviewed_at)
        self.assertEqual(appeal.status, 'upheld')
        self.assertEqual(Comment.objects.count(), 0)

    def test_closed_appeal_invalid_decision_and_empty_note_are_rejected(self):
        event = self.confirmed_event()
        appeal = self.submit_appeal(event)

        self.resolve(appeal, decision='upheld', note='维持原结论。')
        response = self.resolve(appeal, decision='accepted', note='再次处理。')

        self.assertRedirects(response, reverse('moderation_queue'))
        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'upheld')
        self.assertEqual(event.status, 'confirmed')

        second_event = self.confirmed_event(content='第二次加微信校验')
        second_appeal = self.submit_appeal(second_event)
        self.resolve(second_appeal, decision='accepted', note='')
        second_appeal.refresh_from_db()
        self.assertEqual(second_appeal.status, 'pending')

        third_event = self.confirmed_event(content='第三次加微信校验')
        third_appeal = self.submit_appeal(third_event)
        self.resolve(third_appeal, decision='unknown', note='无效决定。')
        third_appeal.refresh_from_db()
        self.assertEqual(third_appeal.status, 'pending')

    def test_non_staff_cannot_resolve_appeal(self):
        event = self.confirmed_event()
        appeal = self.submit_appeal(event)
        self.client.login(username='appeal-buyer', password='safe-password-123')

        response = self.resolve(appeal)

        self.assertEqual(response.status_code, 403)
        appeal.refresh_from_db()
        self.assertEqual(appeal.status, 'pending')


class ModerationAppealAdminTests(ModerationAppealBaseTestCase):
    def test_appeal_admin_is_read_only_without_bulk_actions(self):
        admin = ModerationAppealAdmin(ModerationAppeal, None)

        self.assertIsNone(admin.actions)
        self.assertEqual(admin.has_add_permission(None), False)
        self.assertEqual(admin.has_change_permission(None, None), False)
        self.assertEqual(admin.has_delete_permission(None, None), False)
        self.assertIn('statement', admin.readonly_fields)
        self.assertIn('original_reviewed_by', admin.readonly_fields)

