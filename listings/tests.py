from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse

from .models import BrowsingHistory, CampusLocation, Category, Favorite, Item, Order, Report
from .recommendations import get_recommendations


class ListingFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username='alice', password='safe-password-123')
        self.other_user = User.objects.create_user(username='bob', password='safe-password-123')
        self.category = Category.objects.create(name='数码', description='电子设备')
        self.location = CampusLocation.objects.create(
            name='图书馆东门', building='东校区', address='图书馆一层东侧',
        )
        self.item = Item.objects.create(
            title='便携键盘', description='适合宿舍使用', price='99.00',
            category=self.category, location=self.location, condition='9成新', seller=self.user,
        )

    def test_home_and_listing_pages_render(self):
        self.assertEqual(self.client.get(reverse('home')).status_code, 200)
        response = self.client.get(reverse('item_list'), {'q': '键盘', 'sort': 'price_asc'})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, '便携键盘')
        self.assertContains(response, '图书馆东门')

    def test_location_filter_only_returns_matching_items(self):
        another_location = CampusLocation.objects.create(name='南门快递站')
        Item.objects.create(
            title='宿舍台灯', description='暖光', price='39.00', category=self.category,
            location=another_location, condition='全新', seller=self.user,
        )
        response = self.client.get(reverse('item_list'), {'location': self.location.id})
        self.assertContains(response, '便携键盘')
        self.assertNotContains(response, '宿舍台灯')

    def test_authenticated_user_can_toggle_favorite(self):
        self.client.login(username='alice', password='safe-password-123')
        url = reverse('toggle_favorite', args=[self.item.id])
        response = self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(Favorite.objects.filter(user=self.user, item=self.item).exists())
        self.client.post(url, {'next': reverse('item_detail', args=[self.item.id])})
        self.assertFalse(Favorite.objects.filter(user=self.user, item=self.item).exists())

    def test_user_can_submit_one_report_and_cannot_report_again(self):
        self.client.login(username='bob', password='safe-password-123')
        report_url = reverse('report_item', args=[self.item.id])
        response = self.client.post(report_url, {'reason': 'scam', 'detail': '商品描述与图片不一致'})
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertTrue(Report.objects.filter(item=self.item, reporter=self.other_user, status='pending').exists())
        response = self.client.get(report_url)
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertEqual(Report.objects.filter(item=self.item, reporter=self.other_user).count(), 1)

    def test_seller_cannot_report_own_item(self):
        self.client.login(username='alice', password='safe-password-123')
        response = self.client.get(reverse('report_item', args=[self.item.id]))
        self.assertRedirects(response, reverse('item_detail', args=[self.item.id]))
        self.assertFalse(Report.objects.filter(item=self.item, reporter=self.user).exists())
    def test_buyer_can_place_order_and_seller_can_complete_it(self):
        self.client.login(username='bob', password='safe-password-123')
        order_url = reverse('create_order', args=[self.item.id])
        response = self.client.post(order_url, {'meeting_location': self.location.id, 'buyer_note': '周三晚课后见面'})
        order = Order.objects.get(item=self.item)
        self.assertRedirects(response, reverse('order_detail', args=[order.id]))
        self.item.refresh_from_db()
        self.assertEqual(order.status, 'pending')
        self.assertEqual(self.item.status, 'reserved')

        self.client.logout()
        self.client.login(username='alice', password='safe-password-123')
        status_url = reverse('update_order_status', args=[order.id])
        self.client.post(status_url, {'status': 'confirmed'})
        self.client.post(status_url, {'status': 'meeting'})
        self.client.post(status_url, {'status': 'completed'})
        order.refresh_from_db()
        self.item.refresh_from_db()
        self.assertEqual(order.status, 'completed')
        self.assertEqual(self.item.status, 'sold')
    def test_recommendations_prioritize_matching_category_and_location(self):
        books = Category.objects.create(name='书籍', description='教材资料')
        matching_item = Item.objects.create(
            title='高等数学教材', description='教材', price='45.00', category=self.category,
            location=self.location, condition='9成新', seller=self.other_user,
        )
        unrelated_item = Item.objects.create(
            title='考研资料', description='资料', price='30.00', category=books,
            condition='8成新', seller=self.other_user,
        )
        Favorite.objects.create(user=self.user, item=self.item)
        recommendations = get_recommendations(self.user, limit=2)
        self.assertEqual(recommendations[0].item, matching_item)
        self.assertNotIn(self.item, [recommendation.item for recommendation in recommendations])
        self.assertTrue(recommendations[0].reason)
    def test_health_check_returns_service_status(self):
        response = self.client.get(reverse('health_check'))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['status'], 'ok')
    def test_logged_in_item_view_creates_browsing_history(self):
        self.client.login(username='bob', password='safe-password-123')
        detail_url = reverse('item_detail', args=[self.item.id])
        self.client.get(detail_url)
        self.client.get(detail_url)
        record = BrowsingHistory.objects.get(user=self.other_user, item=self.item)
        self.assertEqual(record.view_count, 2)
        response = self.client.get(reverse('browsing_history'))
        self.assertContains(response, '便携键盘')

