from datetime import timedelta

from django.contrib.auth.models import User
from django.core.exceptions import ValidationError
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .forms import ItemForm
from .models import CampusCampaign, Category, Item


class CampusCampaignTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username='campaign-user', password='safe-password-123',
        )
        self.category = Category.objects.create(name='专题测试分类')
        self.campaign = CampusCampaign.objects.create(
            title='毕业季清仓',
            slug='graduation-clearance',
            description='让毕业季闲置继续流转。',
            starts_at=timezone.now() - timedelta(days=1),
            ends_at=timezone.now() + timedelta(days=7),
        )

    def _item(self, *, status='available', campaign=None, title='毕业季教材'):
        return Item.objects.create(
            title=title,
            description='专题商品测试',
            price='12.00',
            category=self.category,
            condition='良好',
            seller=self.user,
            status=status,
            campaign=campaign,
        )

    def test_campaign_list_only_shows_live_enabled_campaigns(self):
        CampusCampaign.objects.create(
            title='尚未开始',
            slug='future-campaign',
            starts_at=timezone.now() + timedelta(days=1),
        )
        CampusCampaign.objects.create(
            title='已经结束',
            slug='past-campaign',
            starts_at=timezone.now() - timedelta(days=4),
            ends_at=timezone.now() - timedelta(days=1),
        )
        CampusCampaign.objects.create(
            title='已停用',
            slug='disabled-campaign',
            starts_at=timezone.now() - timedelta(days=1),
            is_active=False,
        )

        response = self.client.get(reverse('campaign_list'))

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '毕业季清仓')
        self.assertNotContains(response, '尚未开始')
        self.assertNotContains(response, '已经结束')
        self.assertNotContains(response, '已停用')

    def test_campaign_detail_only_renders_currently_available_items(self):
        self._item(campaign=self.campaign, title='可购买教材')
        self._item(status='sold', campaign=self.campaign, title='已售教材')
        self._item(campaign=None, title='普通商品')

        response = self.client.get(
            reverse('campaign_detail', args=[self.campaign.slug]),
        )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '可购买教材')
        self.assertNotContains(response, '已售教材')
        self.assertNotContains(response, '普通商品')

    def test_campaign_detail_paginates_items_and_keeps_the_full_count(self):
        titles = {f'专题商品 {index:02d}' for index in range(13)}
        for title in titles:
            self._item(campaign=self.campaign, title=title)
        url = reverse('campaign_detail', args=[self.campaign.slug])
        self.client.force_login(self.user)

        first = self.client.get(url)
        second = self.client.get(url, {'page': 2})

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.context['page_obj'].paginator.count, 13)
        self.assertEqual(first.context['page_obj'].number, 1)
        self.assertEqual(second.context['page_obj'].number, 2)
        first_titles = {item.title for item in first.context['items']}
        second_titles = {item.title for item in second.context['items']}
        self.assertEqual(len(first_titles), 12)
        self.assertEqual(len(second_titles), 1)
        self.assertFalse(first_titles & second_titles)
        self.assertEqual(first_titles | second_titles, titles)
        self.assertContains(first, '13 件当前在售')
        self.assertContains(second, '13 件当前在售')
        self.assertContains(first, '第 1 / 2 页')
        self.assertContains(second, '第 2 / 2 页')
        self.assertContains(second, 'value="/listings/campaigns/graduation-clearance/?page=2"')

    def test_campaign_detail_clamps_invalid_and_out_of_range_pages(self):
        for index in range(13):
            self._item(campaign=self.campaign, title=f'分页商品 {index:02d}')
        url = reverse('campaign_detail', args=[self.campaign.slug])

        invalid = self.client.get(url, {'page': 'not-a-number'})
        beyond = self.client.get(url, {'page': 999})

        self.assertEqual(invalid.context['page_obj'].number, 1)
        self.assertEqual(beyond.context['page_obj'].number, 2)

    def test_item_form_can_attach_an_active_campaign(self):
        form = ItemForm(data={
            'title': '加入专题的教材',
            'description': '一本教材',
            'trade_mode': 'sale',
            'price': '10.00',
            'deposit_amount': '0',
            'borrow_days': '7',
            'category': self.category.id,
            'location': '',
            'campaign': self.campaign.id,
            'condition': '九成新',
            'expires_at': '',
        })

        self.assertTrue(form.is_valid(), form.errors)
        self.assertEqual(form.cleaned_data['campaign'], self.campaign)

    def test_campaign_rejects_an_end_before_start(self):
        campaign = CampusCampaign(
            title='时间错误专题',
            slug='invalid-campaign',
            starts_at=timezone.now(),
            ends_at=timezone.now() - timedelta(minutes=1),
        )

        with self.assertRaises(ValidationError):
            campaign.full_clean()
