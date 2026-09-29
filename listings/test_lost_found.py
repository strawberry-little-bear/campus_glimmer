from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .lost_found_matching import find_lost_found_matches, score_lost_found_posts
from .models import CampusLocation, Category, LostFoundLead, LostFoundPost, Notification


class LostFoundFlowTests(TestCase):
    def setUp(self):
        self.now = timezone.now().replace(microsecond=0)
        self.owner = User.objects.create_user(
            username='lost-found-owner', password='safe-password-123',
        )
        self.finder = User.objects.create_user(
            username='lost-found-finder', password='safe-password-123',
        )
        self.category = Category.objects.create(name='失物测试分类')
        self.location = CampusLocation.objects.create(name='失物测试地点')

    def _post(self, *, reporter, post_type, title, description, related_location=True):
        return LostFoundPost.objects.create(
            reporter=reporter,
            post_type=post_type,
            title=title,
            description=description,
            category=self.category,
            location=self.location if related_location else None,
            occurred_at=self.now - timedelta(hours=3),
            expires_at=self.now + timedelta(days=10),
            identifying_features='蓝色贴纸和银色挂件',
        )

    def test_matching_is_explainable_and_ranks_opposite_records(self):
        lost = self._post(
            reporter=self.owner,
            post_type='lost',
            title='图书馆遗失黑色水杯',
            description='黑色保温水杯，杯身有蓝色贴纸。',
        )
        found = self._post(
            reporter=self.finder,
            post_type='found',
            title='捡到黑色水杯',
            description='在图书馆捡到一个黑色水杯，带蓝色贴纸。',
        )

        score, reasons = score_lost_found_posts(lost, found)
        matches = find_lost_found_matches(lost)

        self.assertGreaterEqual(score, 70)
        self.assertIn('物品分类一致', reasons)
        self.assertIn('发生地点一致', reasons)
        self.assertEqual(matches[0]['post'], found)
        self.assertEqual(matches[0]['score'], score)

    def test_publishing_record_notifies_owner_of_possible_match(self):
        existing = self._post(
            reporter=self.finder,
            post_type='found',
            title='捡到黑色水杯',
            description='在图书馆捡到黑色水杯。',
        )
        self.client.login(username=self.owner.username, password='safe-password-123')
        response = self.client.post(reverse('new_lost_found'), {
            'post_type': 'lost',
            'title': '图书馆遗失黑色水杯',
            'description': '黑色保温水杯，杯身有蓝色贴纸。',
            'category': self.category.id,
            'location': self.location.id,
            'occurred_at': (self.now - timedelta(hours=3)).strftime('%Y-%m-%dT%H:%M'),
            'expires_at': (self.now + timedelta(days=10)).strftime('%Y-%m-%dT%H:%M'),
            'identifying_features': '蓝色贴纸和银色挂件',
        })

        self.assertEqual(response.status_code, 302)
        post = LostFoundPost.objects.get(title='图书馆遗失黑色水杯')
        self.assertTrue(Notification.objects.filter(
            recipient=self.finder,
            kind='lost_found_match',
            target_url=reverse('lost_found_detail', args=[post.id]),
        ).exists())
        self.assertNotEqual(existing.reporter_id, post.reporter_id)

    def test_owner_can_accept_private_lead_and_pair_records(self):
        lost = self._post(
            reporter=self.owner,
            post_type='lost',
            title='遗失校园卡套',
            description='黑色卡套，在图书馆附近丢失。',
        )
        found = self._post(
            reporter=self.finder,
            post_type='found',
            title='捡到黑色卡套',
            description='在图书馆附近捡到黑色卡套。',
        )
        self.client.login(username=self.finder.username, password='safe-password-123')
        submitted = self.client.post(reverse('submit_lost_found_lead', args=[lost.id]), {
            'related_post': found.id,
            'message': '卡套内有一张校园卡，可以通过卡套上的贴纸核验。',
        })
        self.assertEqual(submitted.status_code, 302)
        lead = LostFoundLead.objects.get(post=lost, respondent=self.finder)
        self.assertEqual(lead.status, 'pending')
        self.assertTrue(Notification.objects.filter(
            recipient=self.owner, kind='lost_found_lead',
        ).exists())

        self.client.logout()
        self.client.login(username=self.owner.username, password='safe-password-123')
        accepted = self.client.post(reverse('review_lost_found_lead', args=[lead.id, 'accept']))
        self.assertEqual(accepted.status_code, 302)
        lost.refresh_from_db()
        found.refresh_from_db()
        lead.refresh_from_db()
        self.assertEqual(lead.status, 'accepted')
        self.assertEqual(lost.status, 'matched')
        self.assertEqual(found.status, 'matched')
        self.assertEqual(lost.matched_post_id, found.id)
        self.assertEqual(found.matched_post_id, lost.id)
        self.assertTrue(Notification.objects.filter(
            recipient=self.finder,
            kind='lost_found_lead',
            title='失物招领线索已确认',
        ).exists())

    def test_form_and_detail_pages_render_for_authenticated_user(self):
        self.client.login(username=self.owner.username, password='safe-password-123')
        form_response = self.client.get(reverse('new_lost_found'))
        self.assertEqual(form_response.status_code, 200)
        post = self._post(
            reporter=self.owner,
            post_type='lost',
            title='可正常渲染的记录',
            description='用于页面渲染测试',
        )
        detail_response = self.client.get(reverse('lost_found_detail', args=[post.id]))
        self.assertEqual(detail_response.status_code, 200)
        self.assertContains(detail_response, '可正常渲染的记录')

    def test_expired_record_is_not_publicly_listed(self):
        expired = self._post(
            reporter=self.owner,
            post_type='lost',
            title='过期失物',
            description='已经过期的记录',
        )
        LostFoundPost.objects.filter(pk=expired.pk).update(
            expires_at=self.now - timedelta(minutes=1),
        )
        response = self.client.get(reverse('lost_found_list'))
        self.assertEqual(response.status_code, 200)
        expired.refresh_from_db()
        self.assertEqual(expired.status, 'expired')
        self.assertNotContains(response, '过期失物')
