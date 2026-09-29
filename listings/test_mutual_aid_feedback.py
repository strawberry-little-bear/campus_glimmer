from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    CampusLocation,
    Category,
    CommunityContribution,
    DemandPost,
    DemandResponse,
    Item,
    LostFoundLead,
    LostFoundPost,
    MutualAidFeedback,
    Notification,
)


class MutualAidFeedbackTests(TestCase):
    def setUp(self):
        self.owner = User.objects.create_user(
            username='feedback-owner', password='safe-password-123',
        )
        self.helper = User.objects.create_user(
            username='feedback-helper', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='feedback-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='互助反馈分类')
        self.location = CampusLocation.objects.create(name='互助反馈地点')
        self.demand = DemandPost.objects.create(
            requester=self.owner,
            title='求购互助反馈测试教材',
            description='用于测试互助结果反馈。',
            category=self.category,
            location=self.location,
        )
        self.item = Item.objects.create(
            seller=self.helper,
            title='互助反馈测试教材',
            description='保存良好。',
            category=self.category,
            location=self.location,
            condition='9成新',
            price='20.00',
        )
        self.response = DemandResponse.objects.create(
            demand=self.demand,
            item=self.item,
            responder=self.helper,
            status='accepted',
            match_score=4,
            match_reason='分类一致',
        )

    def _submit_demand_feedback(self, user=None, **data):
        self.client.login(
            username=(user or self.owner).username,
            password='safe-password-123',
        )
        payload = {
            'outcome': 'completed',
            'tags': ['on_time', 'smooth'],
            'note': '已在图书馆大厅完成交接。',
        }
        payload.update(data)
        return self.client.post(
            reverse('submit_demand_feedback', args=[self.response.id]),
            payload,
        )

    def test_owner_can_confirm_completed_mutual_aid_once(self):
        response = self._submit_demand_feedback()
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        feedback = MutualAidFeedback.objects.get(demand_response=self.response)
        self.assertEqual(feedback.outcome, 'completed')
        self.assertEqual(feedback.tags, ['on_time', 'smooth'])
        self.assertEqual(feedback.submitted_by_id, self.owner.id)
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.helper, kind='mutual_aid_completed',
            ).count(),
            1,
        )
        self.assertTrue(Notification.objects.filter(
            recipient=self.helper,
            kind='mutual_aid_feedback',
        ).exists())

        duplicate = self._submit_demand_feedback()
        self.assertRedirects(duplicate, reverse('demand_detail', args=[self.demand.id]))
        self.assertEqual(MutualAidFeedback.objects.filter(demand_response=self.response).count(), 1)
        self.assertEqual(
            CommunityContribution.objects.filter(
                user=self.helper, kind='mutual_aid_completed',
            ).count(),
            1,
        )

    def test_non_owner_cannot_submit_feedback(self):
        response = self._submit_demand_feedback(user=self.helper)
        self.assertEqual(response.status_code, 403)
        self.assertFalse(MutualAidFeedback.objects.exists())

    def test_unresolved_feedback_is_recorded_without_completion_points(self):
        response = self._submit_demand_feedback(
            outcome='unresolved', tags=['needs_follow_up'], note='还需要再次确认交付时间。',
        )
        self.assertRedirects(response, reverse('demand_detail', args=[self.demand.id]))
        feedback = MutualAidFeedback.objects.get(demand_response=self.response)
        self.assertEqual(feedback.outcome, 'unresolved')
        self.assertFalse(CommunityContribution.objects.filter(
            user=self.helper, kind='mutual_aid_completed',
        ).exists())

    def test_owner_can_confirm_lost_found_lead_and_helper_sees_feedback(self):
        post = LostFoundPost.objects.create(
            reporter=self.owner,
            post_type='lost',
            title='反馈测试失物',
            description='测试失物招领结果反馈。',
            category=self.category,
            location=self.location,
            occurred_at=timezone.now() - timedelta(hours=2),
        )
        lead = LostFoundLead.objects.create(
            post=post,
            respondent=self.helper,
            message='我在图书馆大厅看到了类似物品。',
            status='accepted',
        )
        self.client.login(username=self.owner.username, password='safe-password-123')
        response = self.client.post(
            reverse('submit_lost_found_feedback', args=[lead.id]),
            {'outcome': 'completed', 'tags': ['helpful'], 'note': ''},
        )
        self.assertRedirects(response, reverse('lost_found_detail', args=[post.id]))
        self.assertTrue(MutualAidFeedback.objects.filter(lost_found_lead=lead).exists())
        self.assertTrue(CommunityContribution.objects.filter(
            user=self.helper, kind='mutual_aid_completed',
        ).exists())

        self.client.login(username=self.helper.username, password='safe-password-123')
        detail = self.client.get(reverse('lost_found_detail', args=[post.id]))
        self.assertEqual(detail.status_code, 200)
        self.assertContains(detail, '已完成')
        self.assertContains(detail, '信息有帮助')
