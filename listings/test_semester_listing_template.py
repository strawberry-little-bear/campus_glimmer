from datetime import timedelta

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import CampusCampaign, CampusLocation, Category, Item, Notification, SemesterListingTemplate


class SemesterListingTemplateTests(TestCase):
    def setUp(self):
        self.seller = User.objects.create_user(
            username='template-seller', password='safe-password-123',
        )
        self.other_user = User.objects.create_user(
            username='template-other', password='safe-password-123',
        )
        self.category = Category.objects.create(name='学期模板分类')
        self.location = CampusLocation.objects.create(name='学生活动中心')
        self.campaign = CampusCampaign.objects.create(
            title='开学教材周',
            slug='semester-textbook-week',
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=20),
        )

    def _datetime_value(self, value):
        return timezone.localtime(value).strftime('%Y-%m-%dT%H:%M')

    def _save_post(self, **overrides):
        data = {
            'semester_template_name': '开学教材周',
            'category': self.category.id,
            'location': self.location.id,
            'campaign': self.campaign.id,
            'expires_at': self._datetime_value(timezone.now() + timedelta(days=10)),
            'form-TOTAL_FORMS': '0',
            'form-INITIAL_FORMS': '0',
            'form-MIN_NUM_FORMS': '0',
            'form-MAX_NUM_FORMS': '20',
        }
        data.update(overrides)
        return data

    def test_template_actions_require_login(self):
        save_url = reverse('bulk_listing_semester_template_save')
        delete_url = reverse(
            'bulk_listing_semester_template_delete', args=[1],
        )

        save_response = self.client.post(save_url, self._save_post())
        delete_response = self.client.post(delete_url)

        self.assertEqual(save_response.status_code, 302)
        self.assertIn('login', save_response.url)
        self.assertEqual(delete_response.status_code, 302)
        self.assertIn('login', delete_response.url)

    def test_owner_can_save_relative_expiry_template_without_creating_items(self):
        self.client.force_login(self.seller)

        response = self.client.post(
            reverse('bulk_listing_semester_template_save'), self._save_post(),
        )

        template = SemesterListingTemplate.objects.get()
        self.assertRedirects(
            response, f"{reverse('bulk_create_items')}?template={template.id}",
        )
        self.assertEqual(template.seller, self.seller)
        self.assertEqual(template.category, self.category)
        self.assertEqual(template.location, self.location)
        self.assertEqual(template.campaign, self.campaign)
        self.assertEqual(template.expiry_days, 10)
        self.assertEqual(Item.objects.count(), 0)
        self.assertEqual(Notification.objects.count(), 0)

    def test_open_ended_expiry_is_saved_as_long_term_template(self):
        self.client.force_login(self.seller)

        self.client.post(
            reverse('bulk_listing_semester_template_save'),
            self._save_post(expires_at=''),
        )

        template = SemesterListingTemplate.objects.get()
        self.assertIsNone(template.expiry_days)

    def test_same_name_overwrites_the_sellers_own_template(self):
        self.client.force_login(self.seller)
        other_template = SemesterListingTemplate.objects.create(
            seller=self.other_user,
            name='开学教材周',
            category=self.category,
            expiry_days=7,
        )
        self.client.post(reverse('bulk_listing_semester_template_save'), self._save_post())
        self.client.post(
            reverse('bulk_listing_semester_template_save'),
            self._save_post(campaign='', expires_at=''),
        )

        own_template = SemesterListingTemplate.objects.get(seller=self.seller)
        self.assertEqual(SemesterListingTemplate.objects.count(), 2)
        self.assertEqual(own_template.expiry_days, None)
        self.assertIsNone(own_template.campaign)
        other_template.refresh_from_db()
        self.assertEqual(other_template.seller, self.other_user)
        self.assertEqual(other_template.expiry_days, 7)

    def test_name_and_expiry_boundaries_are_rejected(self):
        self.client.force_login(self.seller)

        empty_name = self.client.post(
            reverse('bulk_listing_semester_template_save'),
            self._save_post(semester_template_name=' '),
        )
        too_far = self.client.post(
            reverse('bulk_listing_semester_template_save'),
            self._save_post(
                expires_at=self._datetime_value(timezone.now() + timedelta(days=366)),
            ),
        )

        self.assertEqual(empty_name.status_code, 200)
        self.assertEqual(too_far.status_code, 200)
        self.assertFalse(SemesterListingTemplate.objects.exists())

    def test_seller_can_keep_at_most_ten_templates(self):
        for index in range(10):
            SemesterListingTemplate.objects.create(
                seller=self.seller,
                name=f'模板 {index + 1}',
                category=self.category,
                expiry_days=index + 1,
            )
        self.client.force_login(self.seller)

        response = self.client.post(
            reverse('bulk_listing_semester_template_save'),
            self._save_post(semester_template_name='第 11 个模板'),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(SemesterListingTemplate.objects.count(), 10)
        self.assertFalse(
            SemesterListingTemplate.objects.filter(name='第 11 个模板').exists(),
        )

    def test_owner_can_apply_template_and_recalculate_expiry_for_today(self):
        template = SemesterListingTemplate.objects.create(
            seller=self.seller,
            name='开学教材周',
            category=self.category,
            location=self.location,
            campaign=self.campaign,
            expiry_days=21,
        )
        self.client.force_login(self.seller)

        response = self.client.get(reverse('bulk_create_items'), {'template': template.id})
        initial = response.context['settings_form'].initial

        self.assertEqual(response.status_code, 200)
        self.assertEqual(initial['category'], self.category.id)
        self.assertEqual(initial['location'], self.location.id)
        self.assertEqual(initial['campaign'], self.campaign.id)
        self.assertGreaterEqual(initial['expires_at'], timezone.now() + timedelta(days=20))

    def test_inactive_location_and_campaign_are_skipped_when_applying(self):
        template = SemesterListingTemplate.objects.create(
            seller=self.seller,
            name='过期配置模板',
            category=self.category,
            location=self.location,
            campaign=self.campaign,
            expiry_days=7,
        )
        self.location.is_active = False
        self.location.save(update_fields=['is_active'])
        self.campaign.is_active = False
        self.campaign.save(update_fields=['is_active'])
        self.client.force_login(self.seller)

        response = self.client.get(reverse('bulk_create_items'), {'template': template.id})
        initial = response.context['settings_form'].initial

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(initial['location'])
        self.assertIsNone(initial['campaign'])
        self.assertContains(response, '交易地点、校园专题已停用')

    def test_seller_cannot_apply_or_delete_another_users_template(self):
        template = SemesterListingTemplate.objects.create(
            seller=self.other_user,
            name='别人的模板',
            category=self.category,
            expiry_days=7,
        )
        self.client.force_login(self.seller)

        apply_response = self.client.get(
            reverse('bulk_create_items'), {'template': template.id},
        )
        delete_response = self.client.post(
            reverse('bulk_listing_semester_template_delete', args=[template.id]),
        )

        self.assertEqual(apply_response.status_code, 200)
        self.assertNotIn('category', apply_response.context['settings_form'].initial)
        self.assertEqual(delete_response.status_code, 404)
        self.assertTrue(SemesterListingTemplate.objects.filter(pk=template.pk).exists())

    def test_owner_can_delete_template_without_touching_items(self):
        template = SemesterListingTemplate.objects.create(
            seller=self.seller,
            name='待删除模板',
            category=self.category,
        )
        self.client.force_login(self.seller)

        response = self.client.post(
            reverse('bulk_listing_semester_template_delete', args=[template.id]),
        )

        self.assertRedirects(response, reverse('bulk_create_items'))
        self.assertFalse(SemesterListingTemplate.objects.exists())
        self.assertEqual(Item.objects.count(), 0)
