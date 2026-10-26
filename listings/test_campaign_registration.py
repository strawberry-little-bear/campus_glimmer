from datetime import timedelta

from django.contrib.admin.sites import AdminSite
from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import RequestFactory, TestCase
from django.urls import reverse
from django.utils import timezone

from .admin import CampusCampaignAdmin
from .campaign_registration import (
    CampaignRegistrationClosed, join_campaign, leave_campaign,
)
from .models import CampusCampaign, CampaignRegistration


class CampaignRegistrationServiceTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='campaign-registration-user',
            password='safe-password-123',
        )
        self.campaign = self._campaign('毕业季报名专题', 'registration-live')

    def _campaign(self, title, slug, **overrides):
        values = {
            'title': title,
            'slug': slug,
            'starts_at': self.now - timedelta(days=1),
            'ends_at': self.now + timedelta(days=1),
        }
        values.update(overrides)
        return CampusCampaign.objects.create(**values)

    def test_join_and_leave_are_idempotent_and_rejoin_keeps_history(self):
        first = join_campaign(self.user, self.campaign, now=self.now)
        second = join_campaign(
            self.user, self.campaign, now=self.now + timedelta(hours=1),
        )
        self.assertTrue(first.changed)
        self.assertEqual(first.state, 'joined')
        self.assertFalse(second.changed)
        self.assertEqual(second.state, 'already_joined')
        self.assertEqual(CampaignRegistration.objects.count(), 1)
        self.assertEqual(second.registration.joined_at, self.now)

        first_leave = leave_campaign(
            self.user, self.campaign, now=self.now + timedelta(hours=2),
        )
        second_leave = leave_campaign(
            self.user, self.campaign, now=self.now + timedelta(hours=3),
        )
        self.assertTrue(first_leave.changed)
        self.assertEqual(first_leave.state, 'left')
        self.assertFalse(second_leave.changed)
        self.assertEqual(second_leave.state, 'not_joined')
        first_leave.registration.refresh_from_db()
        self.assertEqual(
            first_leave.registration.left_at,
            self.now + timedelta(hours=2),
        )

        rejoined = join_campaign(
            self.user, self.campaign, now=self.now + timedelta(hours=4),
        )
        self.assertTrue(rejoined.changed)
        self.assertEqual(rejoined.state, 'joined')
        self.assertEqual(CampaignRegistration.objects.count(), 2)
        self.assertEqual(
            CampaignRegistration.objects.filter(is_active=True).count(), 1,
        )
        old = CampaignRegistration.objects.get(pk=first_leave.registration.pk)
        self.assertEqual(old.left_at, self.now + timedelta(hours=2))

    def test_closed_campaigns_reject_join_and_leave_changes(self):
        future = self._campaign(
            '尚未开始专题', 'registration-future',
            starts_at=self.now + timedelta(days=1),
            ends_at=self.now + timedelta(days=2),
        )
        ended = self._campaign(
            '已经结束专题', 'registration-ended',
            starts_at=self.now - timedelta(days=2),
            ends_at=self.now - timedelta(days=1),
        )
        disabled = self._campaign(
            '已停用专题', 'registration-disabled', is_active=False,
        )
        for campaign in (future, ended, disabled):
            with self.assertRaises(CampaignRegistrationClosed):
                join_campaign(self.user, campaign, now=self.now)

        join_campaign(self.user, self.campaign, now=self.now)
        self.campaign.ends_at = self.now - timedelta(minutes=1)
        with self.assertRaises(CampaignRegistrationClosed):
            leave_campaign(self.user, self.campaign, now=self.now)
        self.assertEqual(
            CampaignRegistration.objects.get().is_active, True,
        )

    def test_database_constraints_prevent_duplicate_or_inconsistent_state(self):
        CampaignRegistration.objects.create(
            campaign=self.campaign,
            user=self.user,
            joined_at=self.now,
        )
        with self.assertRaises(IntegrityError):
            with transaction.atomic():
                CampaignRegistration.objects.create(
                    campaign=self.campaign,
                    user=self.user,
                    joined_at=self.now,
                )

        with self.assertRaises(ValidationError):
            CampaignRegistration(
                campaign=self.campaign,
                user=self.user,
                is_active=True,
                left_at=self.now,
            ).full_clean()
        with self.assertRaises(ValidationError):
            CampaignRegistration(
                campaign=self.campaign,
                user=self.user,
                is_active=False,
            ).full_clean()


class CampaignRegistrationPageTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.user = User.objects.create_user(
            username='campaign-page-user', password='safe-password-123',
        )
        self.other = User.objects.create_user(
            username='campaign-page-other', password='safe-password-123',
        )
        self.campaign = self._campaign('教材交换周', 'page-textbook-week')
        self.other_campaign = self._campaign(
            '毕业寄卖周', 'page-graduation-sale',
        )

    def _campaign(self, title, slug, **overrides):
        values = {
            'title': title,
            'slug': slug,
            'starts_at': self.now - timedelta(days=1),
            'ends_at': self.now + timedelta(days=5),
        }
        values.update(overrides)
        return CampusCampaign.objects.create(**values)

    def test_anonymous_cannot_change_registration_and_sees_login_entry(self):
        response = self.client.post(
            reverse('join_campaign', args=[self.campaign.slug]),
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn(reverse('login'), response.url)
        self.assertEqual(CampaignRegistration.objects.count(), 0)

        detail = self.client.get(
            reverse('campaign_detail', args=[self.campaign.slug]),
        )
        self.assertContains(detail, '登录后加入')

    def test_get_request_cannot_change_registration(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse('join_campaign', args=[self.campaign.slug]),
        )
        self.assertEqual(response.status_code, 405)
        self.assertEqual(CampaignRegistration.objects.count(), 0)

    def test_join_leave_round_trip_updates_detail_state(self):
        self.client.force_login(self.user)
        detail_url = reverse('campaign_detail', args=[self.campaign.slug])

        joined = self.client.post(
            reverse('join_campaign', args=[self.campaign.slug]),
            {'next': detail_url},
        )
        self.assertRedirects(joined, detail_url)
        self.assertEqual(
            CampaignRegistration.objects.filter(is_active=True).count(), 1,
        )
        detail = self.client.get(detail_url)
        self.assertContains(detail, '1 人已加入')
        self.assertContains(detail, '退出专题')

        left = self.client.post(
            reverse('leave_campaign', args=[self.campaign.slug]),
            {'next': detail_url},
        )
        self.assertRedirects(left, detail_url)
        self.assertEqual(
            CampaignRegistration.objects.filter(is_active=False).count(), 1,
        )
        detail = self.client.get(detail_url)
        self.assertContains(detail, '0 人已加入')
        self.assertContains(detail, '加入专题')

    def test_external_next_url_falls_back_to_campaign_detail(self):
        self.client.force_login(self.user)
        response = self.client.post(
            reverse('join_campaign', args=[self.campaign.slug]),
            {'next': 'https://example.net/phishing'},
        )
        self.assertRedirects(
            response,
            reverse('campaign_detail', args=[self.campaign.slug]),
        )

    def test_public_pages_only_expose_aggregate_registration_count(self):
        CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.other, joined_at=self.now,
        )
        detail = self.client.get(
            reverse('campaign_detail', args=[self.campaign.slug]),
        )
        listing = self.client.get(reverse('campaign_list'))

        self.assertContains(detail, '1 人已加入')
        self.assertContains(listing, '1 人已加入')
        self.assertNotContains(detail, self.other.username)
        self.assertNotContains(listing, self.other.username)

    def test_my_campaigns_only_shows_the_signed_in_users_footprint(self):
        CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.user, joined_at=self.now,
        )
        CampaignRegistration.objects.create(
            campaign=self.other_campaign, user=self.other, joined_at=self.now,
        )
        self.client.force_login(self.user)

        response = self.client.get(reverse('my_campaigns'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.campaign.title)
        self.assertNotContains(response, self.other_campaign.title)

    def test_ended_campaign_remains_in_history_without_leave_action(self):
        registration = CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.user, joined_at=self.now,
        )
        self.campaign.ends_at = self.now - timedelta(minutes=1)
        self.campaign.save(update_fields=['ends_at'])
        self.client.force_login(self.user)

        response = self.client.get(reverse('my_campaigns'))

        self.assertContains(response, self.campaign.title)
        self.assertContains(response, '活动已归档')
        self.assertNotContains(
            response,
            reverse('leave_campaign', args=[self.campaign.slug]),
        )
        registration.refresh_from_db()
        self.assertTrue(registration.is_active)

    def test_history_paginates_without_hiding_active_footprint(self):
        CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.user, joined_at=self.now,
        )
        for index in range(13):
            history_campaign = self._campaign(
                f'历史专题 {index + 1:02d}', f'history-campaign-{index + 1}',
            )
            CampaignRegistration.objects.create(
                campaign=history_campaign,
                user=self.user,
                joined_at=self.now - timedelta(days=index + 2),
                left_at=self.now - timedelta(days=index + 1),
                is_active=False,
            )
        self.client.force_login(self.user)

        first_page = self.client.get(reverse('my_campaigns'))
        second_page = self.client.get(reverse('my_campaigns'), {'page': 2})

        self.assertEqual(first_page.status_code, 200)
        self.assertEqual(first_page.context['history_total'], 13)
        self.assertEqual(first_page.context['history_page'].number, 1)
        self.assertEqual(len(first_page.context['history_registrations']), 12)
        self.assertContains(first_page, self.campaign.title)
        self.assertContains(first_page, '历史专题 01')
        self.assertNotContains(first_page, '历史专题 13')
        self.assertContains(first_page, '?page=2#campaign-history')

        self.assertEqual(second_page.status_code, 200)
        self.assertEqual(second_page.context['history_page'].number, 2)
        self.assertEqual(len(second_page.context['history_registrations']), 1)
        self.assertContains(second_page, self.campaign.title)
        self.assertContains(second_page, '历史专题 13')
        history_return_url = second_page.context['history_return_url']
        self.assertEqual(
            history_return_url,
            reverse('my_campaigns') + '?page=2#campaign-history',
        )
        subscribed = self.client.post(
            reverse('update_campaign_subscription', args=[self.campaign.slug]),
            {
                'notify_updates': 'on',
                'update_frequency': 'weekly',
                'next': history_return_url,
            },
        )
        self.assertEqual(subscribed.status_code, 302)
        self.assertEqual(subscribed.url, history_return_url)

    def test_history_pagination_normalizes_invalid_and_out_of_range_pages(self):
        for index in range(13):
            history_campaign = self._campaign(
                f'历史专题 {index + 1:02d}', f'history-normalize-{index + 1}',
            )
            CampaignRegistration.objects.create(
                campaign=history_campaign,
                user=self.user,
                joined_at=self.now - timedelta(days=index + 2),
                left_at=self.now - timedelta(days=index + 1),
                is_active=False,
            )
        self.client.force_login(self.user)

        invalid = self.client.get(reverse('my_campaigns'), {'page': 'not-a-number'})
        overrun = self.client.get(reverse('my_campaigns'), {'page': 999})

        self.assertEqual(invalid.context['history_page'].number, 1)
        self.assertEqual(overrun.context['history_page'].number, 2)


class CampaignRegistrationAdminTests(TestCase):
    def setUp(self):
        self.now = timezone.now()
        self.admin = User.objects.create_user(
            username='campaign-admin', password='safe-password-123',
            is_staff=True, is_superuser=True,
        )
        self.user = User.objects.create_user(
            username='campaign-admin-participant',
            password='safe-password-123',
        )
        self.campaign = CampusCampaign.objects.create(
            title='后台专题', slug='admin-campaign',
            starts_at=self.now - timedelta(days=1),
            ends_at=self.now + timedelta(days=1),
        )
        self.registration = CampaignRegistration.objects.create(
            campaign=self.campaign, user=self.user, joined_at=self.now,
        )

    def test_registration_changelist_is_available_to_staff(self):
        self.client.force_login(self.admin)
        response = self.client.get(
            reverse('admin:listings_campaignregistration_changelist'),
        )
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, self.campaign.title)
        self.assertContains(response, self.user.username)

    def test_campaign_admin_annotates_active_registration_count(self):
        model_admin = CampusCampaignAdmin(CampusCampaign, AdminSite())
        request = RequestFactory().get('/admin/listings/campuscampaign/')
        request.user = self.admin
        campaign = model_admin.get_queryset(request).get(pk=self.campaign.pk)

        self.assertEqual(model_admin.active_registration_count(campaign), 1)
