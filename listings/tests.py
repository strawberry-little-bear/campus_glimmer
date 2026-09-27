from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import Category, Favorite, Item


class ListingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='safe-password-123')
        self.category = Category.objects.create(name='数码', description='电子设备')
        self.item = Item.objects.create(
            title='便携键盘', description='适合宿舍使用', price='99.00',
            category=self.category, condition='9成新', seller=self.user,
        )

    def test_home_and_listing_pages_render(self):
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)
        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'price_asc'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '便携键盘')

    def test_authenticated_user_can_toggle_favorite(self):
        self.client.login(username='alice', password='safe-password-123')
        url = reverse('toggle_favorite', args=[self.item.id])
        response = self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Favorite.objects.filter(user=self.user, item=self.item).exists())
        self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertFalse(Favorite.objects.filter(user=self.user, item=self.item).exists())
